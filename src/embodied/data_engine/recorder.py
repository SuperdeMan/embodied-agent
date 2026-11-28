"""Episode recording: capture every run as a data asset (docs/architecture.md §4.6).

Recording is enabled by the caller: collect/teleop use it by default, and local
sim and console enable it with --record. Failed episodes are
labeled and retained. The v0 intermediate format (npz + json) maps to
LeRobotDataset; the M2 converter uses optional lerobot/torch dependencies (D014).

Episode directory layout (``<root>/<YYYYMMDD-HHMMSS>-<NNN>/``):

- ``steps.npz``    — step-aligned arrays (timestamp / observation.state / action /
  ee_pos / object/<name>), buffered in memory and written once at finish/abort.
- ``meta.json``    — schema_version, task semantics, result, timing, field map.
  A stub lands at start() so even a hard kill leaves an identifiable episode.
- ``events.jsonl`` — skill/safety/note events, streamed to disk as they happen.


记录器保存 episode。步骤按检查点写入增量日志，结束时生成步骤数组，元数据在开始即落盘，事件逐条写入。字段对齐 LeRobot。collect/teleop 默认录制，本地 sim 和 console 通过 --record 开启。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from embodied.contracts.metrics import EpisodeAuditResult
from embodied.control.hal import ActionCommand, Observation

SCHEMA_VERSION = "v0"
DEFAULT_CHECKPOINT_EVERY = 10
STEP_JOURNAL_FILENAME = "steps.journal.jsonl"

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
        *,
        checkpoint_every: int = DEFAULT_CHECKPOINT_EVERY,
        fsync_checkpoints: bool = False,
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
                episode_dir,
                task=task,
                embodiment_id=embodiment_id,
                seed=seed,
                extra_meta=extra_meta,
                checkpoint_every=checkpoint_every,
                fsync_checkpoints=fsync_checkpoints,
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
        checkpoint_every: int = DEFAULT_CHECKPOINT_EVERY,
        fsync_checkpoints: bool = False,
    ) -> None:
        if type(checkpoint_every) is not int or checkpoint_every < 1:
            raise ValueError("checkpoint_every must be a positive integer")
        self.path = Path(episode_dir)
        self._task = task
        self._embodiment_id = embodiment_id
        self._seed = seed
        self._extra_meta = dict(extra_meta or {})
        self._checkpoint_every = checkpoint_every
        self._fsync_checkpoints = bool(fsync_checkpoints)
        self._started_at = _now_iso()
        self._closed = False

        self._timestamps: list[float] = []
        self._states: list[tuple[float, ...]] = []
        self._actions: list[tuple[float, ...]] = []
        self._ee_pos: list[tuple[float, ...]] = []
        self._objects: dict[str, list[tuple[float, ...]]] = {}
        self._state_dim: int | None = None
        self._command_identity: tuple[str, str] | None = None
        self._journal_path = self.path / STEP_JOURNAL_FILENAME
        self._pending_journal: list[str] = []

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
        envelope = action.envelope if action is not None else None
        if envelope is not None:
            identity = (envelope.operation_id, envelope.attempt_id)
            if identity != self._command_identity:
                self.on_event("note", {"kind": "action_authority", "sim_t": obs.t,
                                       "operation_id": envelope.operation_id, "attempt_id": envelope.attempt_id,
                                       "source": envelope.source, "lease_epoch": envelope.lease_epoch,
                                       "profile_digest": envelope.profile_digest, "scene_epoch": envelope.scene_epoch})
                self._command_identity = identity
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

        # 按周期把步骤增量刷盘至 steps.journal.jsonl，将进程硬杀损失严格收敛到 < checkpoint_every 步。
        # Periodically flush step rows to steps.journal.jsonl so an abrupt kill loses < checkpoint_every steps.
        step_objs = {
            name: [float(v) for v in (*pose.pos, *pose.quat)]
            for name, pose in obs.objects.items()
        }
        journal_row = json.dumps(
            {
                "t": float(obs.t),
                "state": list(state),
                "action": list(row),
                "ee_pos": list(self._ee_pos[-1]),
                "objects": step_objs,
            },
            separators=(",", ":"),
            allow_nan=False,
        )
        self._pending_journal.append(journal_row)
        if len(self._pending_journal) >= self._checkpoint_every:
            self._flush_journal()

    def _flush_journal(self) -> None:
        if not self._pending_journal:
            return
        with open(self._journal_path, "a", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(self._pending_journal) + "\n")
            f.flush()
            if self._fsync_checkpoints:
                os.fsync(f.fileno())
        self._pending_journal.clear()

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

    def finish_unjudged(self, detail: str) -> Path:
        """Finalize a recording without a task-level judge. 保存缺少任务级裁判的完整录制。"""
        self._ensure_open("finish_unjudged")
        return self._finalize(success=None, detail=detail, aborted=False)

    def _finalize(self, *, success: bool | None, detail: str, aborted: bool) -> Path:
        # 先写数组再写最终元数据；成功标记意味着数据已完整写出，最后清理增量日志。
        # Arrays first, meta last: a meta.json with success set implies a complete episode, then remove journal.
        steps_sha256 = self._write_steps()
        self._write_meta(
            success=success, detail=detail, finished_at=_now_iso(), aborted=aborted, steps_sha256=steps_sha256,
        )
        self._pending_journal.clear()
        if self._journal_path.exists():
            try:
                self._journal_path.unlink()
            except OSError:
                pass
        self._closed = True
        return self.path

    def _write_steps(self) -> str:
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
        digest = hashlib.sha256(tmp.read_bytes()).hexdigest()
        os.replace(tmp, self.path / "steps.npz")
        return digest

    def _write_meta(
        self,
        *,
        success: bool | None,
        detail: str,
        finished_at: str | None,
        aborted: bool,
        steps_sha256: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
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
            "checkpoint_every": self._checkpoint_every,
            "extra_meta": self._extra_meta,
            "lerobot_field_map": LEROBOT_FIELD_MAP,
        }
        if success is False:
            from embodied.data_engine.failures import failure_category

            payload["failure_category"] = failure_category(detail, aborted=aborted)
        if steps_sha256 is not None:
            payload["steps_sha256"] = steps_sha256
        _write_json(self.path / "meta.json", payload)

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


def audit_episode(path: Path | str) -> EpisodeAuditResult:
    """Read-only integrity check for a recorded episode directory (D035).

    对单个录制 episode 目录执行只读完整性审计（校验元数据、steps_sha256、维度对齐、有限值与时间单调性）。
    """
    episode_dir = Path(path)
    issues: list[str] = []
    meta_path = episode_dir / "meta.json"
    steps_path = episode_dir / "steps.npz"
    journal_path = episode_dir / STEP_JOURNAL_FILENAME
    events_path = episode_dir / "events.jsonl"

    if not meta_path.is_file():
        return EpisodeAuditResult(
            episode_dir=str(episode_dir),
            intact=False,
            needs_recovery=journal_path.is_file(),
            recorded_steps=0,
            steps_sha256_verified=False,
            events_count=0,
            issues=("missing_meta_json",),
        )
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if not isinstance(meta, dict):
            raise ValueError("meta must be object")
    except Exception:
        return EpisodeAuditResult(
            episode_dir=str(episode_dir),
            intact=False,
            needs_recovery=journal_path.is_file(),
            recorded_steps=0,
            steps_sha256_verified=False,
            events_count=0,
            issues=("corrupt_meta_json",),
        )

    events_count = 0
    if events_path.is_file():
        for line in events_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
                if not isinstance(parsed, dict) or parsed.get("kind") not in EVENT_KINDS:
                    issues.append("invalid_event_record")
                    break
                events_count += 1
            except Exception:
                issues.append("corrupt_events_jsonl")
                break

    unfinalized = meta.get("finished_at") is None
    if unfinalized:
        issues.append("unfinalized_meta")
    if not steps_path.is_file():
        issues.append("missing_steps_npz")
        return EpisodeAuditResult(
            episode_dir=str(episode_dir),
            intact=False,
            needs_recovery=True,
            recovered_from_crash=bool(meta.get("recovered_from_crash", False)),
            recorded_steps=0,
            steps_sha256_verified=False,
            events_count=events_count,
            issues=tuple(issues),
        )

    sha_ok = False
    expected_sha = meta.get("steps_sha256")
    if expected_sha is not None:
        actual_sha = hashlib.sha256(steps_path.read_bytes()).hexdigest()
        sha_ok = actual_sha == expected_sha
        if not sha_ok:
            issues.append("steps_sha256_mismatch")

    recorded_steps = 0
    try:
        with np.load(steps_path) as z:
            required = {"timestamp", "observation.state", "action", "ee_pos"}
            if not required.issubset(set(z.files)):
                issues.append("missing_required_arrays")
            else:
                ts = z["timestamp"]
                state = z["observation.state"]
                action = z["action"]
                ee_pos = z["ee_pos"]
                recorded_steps = int(ts.shape[0]) if ts.ndim == 1 else 0
                if ts.ndim != 1:
                    issues.append("bad_timestamp_ndim")
                if recorded_steps != meta.get("length"):
                    issues.append("length_mismatch")
                if state.ndim != 2 or action.ndim != 2 or state.shape != action.shape or state.shape[0] != recorded_steps:
                    issues.append("state_action_shape_mismatch")
                if ee_pos.shape != (recorded_steps, 3):
                    issues.append("ee_pos_shape_mismatch")
                if not (np.isfinite(ts).all() and np.isfinite(state).all() and np.isfinite(action).all() and np.isfinite(ee_pos).all()):
                    issues.append("nonfinite_step_values")
                if recorded_steps >= 2 and bool(np.any(np.diff(ts) < -1e-6)):
                    issues.append("non_monotonic_timestamps")
                for key in z.files:
                    if key.startswith("object/") and z[key].shape != (recorded_steps, _POSE_DIM):
                        issues.append(f"object_shape_mismatch:{key}")
    except Exception:
        issues.append("corrupt_steps_npz")

    return EpisodeAuditResult(
        episode_dir=str(episode_dir),
        intact=not issues,
        needs_recovery=unfinalized,
        recovered_from_crash=bool(meta.get("recovered_from_crash", False)),
        recorded_steps=recorded_steps,
        steps_sha256_verified=sha_ok,
        events_count=events_count,
        issues=tuple(issues),
    )


def _valid_finite_seq(values: Any, length: int | None = None) -> bool:
    if not isinstance(values, list):
        return False
    if length is not None and len(values) != length:
        return False
    return all(type(v) in (int, float) and math.isfinite(float(v)) for v in values)


def replay_episode(path: Path | str, *, frame: int | None = None) -> dict:
    """Read the event timeline and an optional sample after integrity checks. 校验录制后读取事件时间线及指定采样。"""
    audit = audit_episode(path)
    if not audit.intact:
        return {"ok": False, "issues": list(audit.issues), "needs_recovery": audit.needs_recovery}
    episode = load_episode(path)
    try:
        json.dumps(episode.events, allow_nan=False)
    except ValueError:
        return {"ok": False, "issues": ["non_finite_event_value"]}
    if frame is not None and not 0 <= frame < len(episode):
        return {"ok": False, "issues": ["frame_out_of_range"], "samples": len(episode)}
    report = {"ok": True, "episode_id": episode.path.name, "samples": len(episode),
              "success": episode.meta.get("success"), "aborted": episode.meta.get("aborted"),
              "events": episode.events}
    if frame is not None:
        report["frame"] = {
            "index": frame, "time": float(episode.timestamp[frame]), "state": episode.state[frame].tolist(),
            "action": episode.action[frame].tolist(), "ee_pos": episode.ee_pos[frame].tolist(),
            "objects": {name: poses[frame].tolist() if np.isfinite(poses[frame]).all() else None
                        for name, poses in episode.objects.items()},
        }
    return report


def recover_episode(path: Path | str, *, detail: str = "recovered from abrupt crash journal") -> Episode:
    """Recover an abruptly killed episode from steps.journal.jsonl into a valid v0 episode (D035).

    从进程硬杀残留的 steps.journal.jsonl 重建完整 v0 episode（容忍截断尾行），标明 aborted 与 recovered_from_crash。
    """
    episode_dir = Path(path)
    meta_path = episode_dir / "meta.json"
    steps_path = episode_dir / "steps.npz"
    journal_path = episode_dir / STEP_JOURNAL_FILENAME

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if steps_path.is_file() and meta.get("finished_at") is not None:
        return load_episode(episode_dir)

    timestamps: list[float] = []
    states: list[tuple[float, ...]] = []
    actions: list[tuple[float, ...]] = []
    ee_pos: list[tuple[float, ...]] = []
    objects: dict[str, list[tuple[float, ...]]] = {}
    state_dim: int | None = None
    dropped_corrupt_tail_lines = 0

    if journal_path.is_file():
        raw_lines = journal_path.read_bytes().splitlines()
        for raw_line in raw_lines:
            if not raw_line.strip():
                continue
            try:
                rec = json.loads(raw_line.decode("utf-8"))
                if not isinstance(rec, dict):
                    raise ValueError("row must be object")
                t_val = rec["t"]
                if type(t_val) not in (int, float) or not math.isfinite(float(t_val)):
                    raise ValueError("bad t")
                st = rec["state"]
                act = rec["action"]
                ee = rec["ee_pos"]
                objs = rec.get("objects", {})
                if not (_valid_finite_seq(st) and _valid_finite_seq(act, len(st)) and _valid_finite_seq(ee, 3)):
                    raise ValueError("bad vectors")
                if not isinstance(objs, dict):
                    raise ValueError("bad objects")
                for pose_vals in objs.values():
                    if not _valid_finite_seq(pose_vals, _POSE_DIM):
                        raise ValueError("bad object pose")
                if state_dim is None:
                    state_dim = len(st)
                elif len(st) != state_dim:
                    raise ValueError("state dim mismatch")
            except Exception:
                # 仅保留最后一条损坏行之前的连续完整前缀，避免拼接失齐的时间轴。
                # Keep the longest valid contiguous prefix before any torn/corrupt write.
                dropped_corrupt_tail_lines += 1
                break

            idx = len(timestamps)
            timestamps.append(float(t_val))
            states.append(tuple(float(v) for v in st))
            actions.append(tuple(float(v) for v in act))
            ee_pos.append(tuple(float(v) for v in ee))
            for name, pose_vals in objs.items():
                rows = objects.setdefault(str(name), [])
                if len(rows) < idx:
                    rows.extend([_NAN_POSE] * (idx - len(rows)))
                rows.append(tuple(float(v) for v in pose_vals))
            for rows in objects.values():
                if len(rows) == idx:
                    rows.append(_NAN_POSE)

    n = len(timestamps)
    dim = state_dim or 0
    arrays: dict[str, np.ndarray] = {
        "timestamp": np.asarray(timestamps, dtype=np.float32),
        "observation.state": np.asarray(states, dtype=np.float32).reshape(n, dim),
        "action": np.asarray(actions, dtype=np.float32).reshape(n, dim),
        "ee_pos": np.asarray(ee_pos, dtype=np.float32).reshape(n, 3),
    }
    for name, rows in objects.items():
        arrays[f"object/{name}"] = np.asarray(rows, dtype=np.float32).reshape(n, _POSE_DIM)

    tmp = episode_dir / "steps.tmp.npz"
    np.savez_compressed(tmp, **arrays)
    digest = hashlib.sha256(tmp.read_bytes()).hexdigest()
    os.replace(tmp, steps_path)

    updated_meta = dict(meta)
    updated_meta.update({
        "success": False,
        "aborted": True,
        "recovered_from_crash": True,
        "failure_category": "interrupted",
        "dropped_corrupt_tail_lines": dropped_corrupt_tail_lines,
        "detail": detail,
        "length": n,
        "finished_at": _now_iso(),
        "steps_sha256": digest,
    })
    _write_json(meta_path, updated_meta)
    if journal_path.is_file():
        try:
            journal_path.unlink()
        except OSError:
            pass
    return load_episode(episode_dir)


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
