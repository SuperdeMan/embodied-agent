"""Episode recording: capture every run as a data asset (docs/architecture.md §4.6).

Recording is enabled by the caller: collect/teleop use it by default, local sim
requires --record, and console integration remains pending. Failed episodes are
labeled and retained. The v0 intermediate format (npz + json) maps to
LeRobotDataset; the M2 converter uses optional lerobot/torch dependencies (D014).

Episode directory layout (``<root>/<YYYYMMDD-HHMMSS>-<NNN>/``):

- ``steps.npz``    — step-aligned arrays (timestamp / observation.state / action /
  ee_pos / object/<name>), buffered in memory and written once at finish/abort.
- ``meta.json``    — schema_version, task semantics, result, timing, field map.
  A stub lands at start() so even a hard kill leaves an identifiable episode.
- ``events.jsonl`` — skill/safety/note events, streamed to disk as they happen.


记录器保存 episode：步骤数组在结束或中止时写出，元数据在开始即落盘，事件逐条写入。字段对齐 LeRobot；是否开启由调用入口决定，当前并非所有入口默认开启。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from embodied.control.hal import ActionCommand, Observation

SCHEMA_VERSION = "v0"

# 仅允许已声明事件类型，其他类型是调用方错误。
# The only event kinds the runtime may emit; anything else is a caller bug.
EVENT_KINDS = frozenset({"skill_start", "skill_end", "safety", "note"})

# 轻量字段映射到 LeRobot v3，并写入每份元数据保证自描述。
# v0 field name -> LeRobotDataset v3 field name (D003: align, don't invent).
# Embedded verbatim in every meta.json so episodes stay self-describing.
LEROBOT_FIELD_MAP = {
    "observation.state": "observation.state",
    "action": "action",
    "timestamp": "timestamp",
    "meta.task": "tasks",
}

# 三维位置与 wxyz 四元数。
_POSE_DIM = 7  # xyz position + wxyz quaternion
_NAN_POSE = (float("nan"),) * _POSE_DIM


def _stamp() -> str:
    """Local-time directory stamp (monkeypatched in tests to force collisions).

    本地时间目录戳，测试可替换以制造同秒冲突。
    """
    return time.strftime("%Y%m%d-%H%M%S")


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _write_json(path: Path, data: dict[str, Any]) -> None:
    """Atomic JSON write: a crash mid-write must never clobber an existing good file.

    JSON 原子写入，避免崩溃破坏已有有效文件。
    """
    tmp = path.with_suffix(".tmp")
    # 收尾时不可序列化元数据退化为字符串，避免丢失整局。
    # default=str: finalizing is the crash path — a non-serializable meta value must
    # degrade to its string form, not lose the episode.
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


class EpisodeRecorder:
    """Session factory: resolves the episodes root and opens one writer per run.

    Root resolution order: explicit ``root`` argument > ``EPISODES_DIR`` env var >
    ``outputs/episodes`` (relative to cwd; ``outputs/`` is gitignored). Directories
    are created lazily on the first ``start()``.
    

    会话工厂按显式根目录、环境变量、默认 outputs 路径顺序解析，首次开始时才创建目录。
    """

    def __init__(self, root: Path | str | None = None) -> None:
        if root is None:
            root = os.environ.get("EPISODES_DIR") or "outputs/episodes"
        self.root = Path(root)

    def start(
        self,
        task: str,
        embodiment_id: str,
        seed: int | None = None,
        extra_meta: dict[str, Any] | None = None,
    ) -> EpisodeWriter:
        """Open a new episode directory ``<stamp>-<NNN>`` and return its writer.

        The 3-digit sequence increments while the candidate directory exists, so
        several episodes started within the same wall-clock second never collide
        (``mkdir(exist_ok=False)`` makes the claim atomic).
        

        创建时间戳加序号的唯一目录；以原子 mkdir 避免同秒多局碰撞。
        """
        stamp = _stamp()
        for seq in range(1, 1000):
            episode_dir = self.root / f"{stamp}-{seq:03d}"
            try:
                episode_dir.mkdir(parents=True, exist_ok=False)
            except FileExistsError:
                continue
            return EpisodeWriter(
                episode_dir, task=task, embodiment_id=embodiment_id, seed=seed, extra_meta=extra_meta
            )
        raise RuntimeError(f"episode sequence exhausted for stamp {stamp} under {self.root}")


class EpisodeWriter:
    """Buffers steps in memory, streams events to disk, finalizes to steps.npz + meta.json.

    Created via ``EpisodeRecorder.start()``. After ``finish()``/``abort()`` the writer
    is closed: ``on_step``/``on_event``/``finish`` raise; ``abort`` stays a safe no-op
    so it can live in ``except``/``finally`` blocks without guards.
    

    步骤缓存在内存，事件逐条落盘；结束后写操作报错，但 abort 保持幂等以便异常清理。
    """

    def __init__(
        self,
        episode_dir: Path | str,
        *,
        task: str,
        embodiment_id: str,
        seed: int | None = None,
        extra_meta: dict[str, Any] | None = None,
    ) -> None:
        self.path = Path(episode_dir)
        self._task = task
        self._embodiment_id = embodiment_id
        self._seed = seed
        self._extra_meta = dict(extra_meta or {})
        self._started_at = _now_iso()
        self._closed = False

        self._timestamps: list[float] = []
        self._states: list[tuple[float, ...]] = []
        self._actions: list[tuple[float, ...]] = []
        self._ee_pos: list[tuple[float, ...]] = []
        self._objects: dict[str, list[tuple[float, ...]]] = {}
        self._state_dim: int | None = None

        # 开始即写元数据草稿，即使强杀也能关联任务与已写事件。
        # Meta stub on disk immediately: a hard kill (no chance to call abort())
        # still leaves task identity next to the streamed events.jsonl.
        self._write_meta(success=None, detail="", finished_at=None, aborted=False)

    # 控制步骤采集。
    # -- step capture -------------------------------------------------------------

    def on_step(self, obs: Observation, action: ActionCommand | None) -> None:
        """Buffer one control tick.

        ``observation.state`` = qpos + gripper_opening; ``action`` = joint_targets +
        gripper. A ``None`` action carries the previous action row forward (zeros
        before the first); a ``None`` gripper ("hold" in HAL semantics) carries the
        previous gripper target forward. End-effector position and object poses
        (pos + wxyz quat) ride along as extra arrays.
        

        缓冲一个控制周期：状态为关节加夹爪，动作为空沿用上一行，夹爪空值沿用目标；首动作前为零，同时存末端及物体位姿。
        """
        self._ensure_open("on_step")
        state = (*(float(v) for v in obs.qpos), float(obs.gripper_opening))
        if self._state_dim is None:
            self._state_dim = len(state)
        elif len(state) != self._state_dim:
            raise ValueError(f"observation.state dim changed mid-episode: {len(state)} != {self._state_dim}")

        if action is None:
            row = self._actions[-1] if self._actions else (0.0,) * self._state_dim
        else:
            if action.gripper is None:
                grip = self._actions[-1][-1] if self._actions else 0.0
            else:
                grip = float(action.gripper)
            row = (*(float(v) for v in action.joint_targets), grip)
            if len(row) != self._state_dim:
                raise ValueError(
                    f"action dim {len(row)} != observation.state dim {self._state_dim} "
                    "(joint_targets must cover the same joints as qpos, plus gripper)"
                )

        idx = len(self._timestamps)
        self._timestamps.append(float(obs.t))
        self._states.append(state)
        self._actions.append(row)
        self._ee_pos.append(tuple(float(v) for v in obs.ee_pose.pos))
        for name, pose in obs.objects.items():
            rows = self._objects.setdefault(name, [])
            # 中途首次观测到物体，其此前历史用 NaN 补齐。
            if len(rows) < idx:  # object first seen mid-episode: NaN-pad its history
                rows.extend([_NAN_POSE] * (idx - len(rows)))
            rows.append((*(float(v) for v in pose.pos), *(float(v) for v in pose.quat)))
        for rows in self._objects.values():
            # 本步物体缺失用 NaN 保持对齐。
            if len(rows) == idx:  # object absent this step: NaN-pad to stay step-aligned
                rows.append(_NAN_POSE)

    # 事件采集。
    # -- event capture ------------------------------------------------------------

    def on_event(self, kind: str, payload: dict[str, Any] | None = None) -> None:
        """Append one event to events.jsonl immediately (streamed, crash-durable).

        ``kind`` must be one of ``EVENT_KINDS``. Each line carries ``t`` (wall clock,
        epoch seconds) and, when the payload provides it, ``sim_t`` promoted to the
        top level; the remaining payload nests under ``payload``.
        

        立即追加事件，包含墙钟、可选顶层仿真时间和载荷；事件类型必须在许可集合中。
        """
        self._ensure_open("on_event")
        if kind not in EVENT_KINDS:
            raise ValueError(f"unknown event kind {kind!r}; allowed: {sorted(EVENT_KINDS)}")
        body = dict(payload or {})
        record: dict[str, Any] = {"t": time.time(), "kind": kind}
        if "sim_t" in body:
            record["sim_t"] = body.pop("sim_t")
        record["payload"] = body
        # 低频事件每次打开、追加并关闭，降低后续崩溃丢失风险。
        # Open-append-close per event: low rate, and every event survives a later crash.
        with open(self.path / "events.jsonl", "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    # 记录收尾。
    # -- finalization -------------------------------------------------------------

    def finish(self, success: bool, detail: str = "") -> Path:
        """Write steps.npz + final meta.json and close the writer; returns the episode dir.

        写步骤数组及最终元数据，关闭记录器并返回目录。
        """
        self._ensure_open("finish")
        return self._finalize(success=bool(success), detail=detail, aborted=False)

    def abort(self, reason: str = "") -> Path:
        """Crash path: finalize with success=False, keeping every buffered step.

        Safe to call unconditionally from ``except``/``finally`` — a no-op returning
        the episode dir when the writer is already closed.
        

        异常路径保留全部缓冲步骤并标失败；重复中止安全返回原目录。
        """
        if self._closed:
            return self.path
        return self._finalize(success=False, detail=reason, aborted=True)

    def _finalize(self, *, success: bool, detail: str, aborted: bool) -> Path:
        # 先写数组再写最终元数据；成功标记意味着数据已完整写出。
        # Arrays first, meta last: a meta.json with success set implies a complete episode.
        self._write_steps()
        self._write_meta(success=success, detail=detail, finished_at=_now_iso(), aborted=aborted)
        self._closed = True
        return self.path

    def _write_steps(self) -> None:
        n = len(self._timestamps)
        dim = self._state_dim or 0
        arrays: dict[str, np.ndarray] = {
            "timestamp": np.asarray(self._timestamps, dtype=np.float32),
            "observation.state": np.asarray(self._states, dtype=np.float32).reshape(n, dim),
            "action": np.asarray(self._actions, dtype=np.float32).reshape(n, dim),
            "ee_pos": np.asarray(self._ee_pos, dtype=np.float32).reshape(n, 3),
        }
        for name, rows in self._objects.items():
            arrays[f"object/{name}"] = np.asarray(rows, dtype=np.float32).reshape(n, _POSE_DIM)
        # 显式文件名避免 numpy 自动追加后缀。
        tmp = self.path / "steps.tmp.npz"  # named so numpy appends no extra suffix
        np.savez_compressed(tmp, **arrays)
        os.replace(tmp, self.path / "steps.npz")

    def _write_meta(self, *, success: bool | None, detail: str, finished_at: str | None, aborted: bool) -> None:
        _write_json(
            self.path / "meta.json",
            {
                "schema_version": SCHEMA_VERSION,
                "task": self._task,
                "embodiment_id": self._embodiment_id,
                "seed": self._seed,
                "success": success,
                "detail": detail,
                "length": len(self._timestamps),
                "started_at": self._started_at,
                "finished_at": finished_at,
                "aborted": aborted,
                "extra_meta": self._extra_meta,
                "lerobot_field_map": LEROBOT_FIELD_MAP,
            },
        )

    def _ensure_open(self, op: str) -> None:
        if self._closed:
            raise RuntimeError(
                f"{op}() on finalized episode {self.path.name!r}; open a new one via EpisodeRecorder.start()"
            )


# 记录回读。
# -- reading back ------------------------------------------------------------------


# 数组字段无法自动进行布尔等价判断，因此关闭自动相等比较。
@dataclass(eq=False)  # eq=False: ndarray fields make generated __eq__ ambiguous
class Episode:
    """One recorded episode loaded back into memory (replay / tests / eval).

    载入内存的单局记录，供回放、测试和评测。
    """

    path: Path
    meta: dict[str, Any]
    # 本体秒时钟的一维 float32 数组。
    timestamp: np.ndarray  # (n,) float32, embodiment clock seconds
    # 状态数组包含各关节及夹爪。
    state: np.ndarray  # (n, dof+1) float32 — stored as "observation.state"
    # 动作数组包含各关节及夹爪。
    action: np.ndarray  # (n, dof+1) float32
    # 三维位置数组。
    ee_pos: np.ndarray  # (n, 3) float32
    # 物体名映射为位置四元数数组，缺测用 NaN。
    objects: dict[str, np.ndarray]  # name -> (n, 7) float32 pos+quat, NaN where absent
    events: list[dict[str, Any]]

    def __len__(self) -> int:
        return int(self.timestamp.shape[0])


def load_episode(path: Path | str) -> Episode:
    """Load one episode directory written by EpisodeWriter.

    读取记录器生成的单局目录。
    """
    episode_dir = Path(path)
    meta = json.loads((episode_dir / "meta.json").read_text(encoding="utf-8"))
    with np.load(episode_dir / "steps.npz") as z:
        timestamp = z["timestamp"]
        state = z["observation.state"]
        action = z["action"]
        ee_pos = z["ee_pos"]
        objects = {k.split("/", 1)[1]: z[k] for k in z.files if k.startswith("object/")}
    events: list[dict[str, Any]] = []
    events_path = episode_dir / "events.jsonl"
    if events_path.exists():
        for line in events_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))
    return Episode(
        path=episode_dir,
        meta=meta,
        timestamp=timestamp,
        state=state,
        action=action,
        ee_pos=ee_pos,
        objects=objects,
        events=events,
    )


# LeRobot 数据转换。
# -- LeRobot conversion (M2) -------------------------------------------------------


def to_lerobot(episodes_root: Path | str, out_dir: Path | str, **kwargs: Any):
    """Convert v0 episodes under ``episodes_root`` into a LeRobotDataset at ``out_dir``.

    Thin delegate to :func:`embodied.data_engine.lerobot_convert.convert_episodes`
    (docs/decisions.md D014); the lazy import keeps this module free of learn-group
    dependencies. Field mapping: ``LEROBOT_FIELD_MAP`` plus
    ``observation.environment_state`` built from the ``object/<name>`` arrays.
    

    延迟委托 LeRobot 转换器，避免默认导入学习依赖；物体数组额外组成环境状态向量。
    """
    from embodied.data_engine.lerobot_convert import convert_episodes

    return convert_episodes(episodes_root, out_dir, **kwargs)
