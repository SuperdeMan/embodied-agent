"""Episode recording: capture every run as a data asset (docs/architecture.md §4.6).

Recording is the runtime's DEFAULT behavior: sim, teleoperation and autonomous runs
all produce an episode directory; failures are labeled, not discarded — failure data
feeds RL-style post-training. The v0 on-disk format is a lightweight intermediate
(npz + json, no heavy deps) whose field names map 1:1 onto LeRobotDataset
(docs/decisions.md D003); the actual converter ships in M2 once the lerobot/torch
dependencies land (D012). See `to_lerobot` for the exact field mapping.

Episode directory layout (``<root>/<YYYYMMDD-HHMMSS>-<NNN>/``):

- ``steps.npz``    — step-aligned arrays (timestamp / observation.state / action /
  ee_pos / object/<name>), buffered in memory and written once at finish/abort.
- ``meta.json``    — schema_version, task semantics, result, timing, field map.
  A stub lands at start() so even a hard kill leaves an identifiable episode.
- ``events.jsonl`` — skill/safety/note events, streamed to disk as they happen.
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

# The only event kinds the runtime may emit; anything else is a caller bug.
EVENT_KINDS = frozenset({"skill_start", "skill_end", "safety", "note"})

# v0 field name -> LeRobotDataset v3 field name (D003: align, don't invent).
# Embedded verbatim in every meta.json so episodes stay self-describing.
LEROBOT_FIELD_MAP = {
    "observation.state": "observation.state",
    "action": "action",
    "timestamp": "timestamp",
    "meta.task": "tasks",
}

_POSE_DIM = 7  # xyz position + wxyz quaternion
_NAN_POSE = (float("nan"),) * _POSE_DIM


def _stamp() -> str:
    """Local-time directory stamp (monkeypatched in tests to force collisions)."""
    return time.strftime("%Y%m%d-%H%M%S")


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _write_json(path: Path, data: dict[str, Any]) -> None:
    """Atomic JSON write: a crash mid-write must never clobber an existing good file."""
    tmp = path.with_suffix(".tmp")
    # default=str: finalizing is the crash path — a non-serializable meta value must
    # degrade to its string form, not lose the episode.
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


class EpisodeRecorder:
    """Session factory: resolves the episodes root and opens one writer per run.

    Root resolution order: explicit ``root`` argument > ``EPISODES_DIR`` env var >
    ``outputs/episodes`` (relative to cwd; ``outputs/`` is gitignored). Directories
    are created lazily on the first ``start()``.
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

        # Meta stub on disk immediately: a hard kill (no chance to call abort())
        # still leaves task identity next to the streamed events.jsonl.
        self._write_meta(success=None, detail="", finished_at=None, aborted=False)

    # -- step capture -------------------------------------------------------------

    def on_step(self, obs: Observation, action: ActionCommand | None) -> None:
        """Buffer one control tick.

        ``observation.state`` = qpos + gripper_opening; ``action`` = joint_targets +
        gripper. A ``None`` action carries the previous action row forward (zeros
        before the first); a ``None`` gripper ("hold" in HAL semantics) carries the
        previous gripper target forward. End-effector position and object poses
        (pos + wxyz quat) ride along as extra arrays.
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
            if len(rows) < idx:  # object first seen mid-episode: NaN-pad its history
                rows.extend([_NAN_POSE] * (idx - len(rows)))
            rows.append((*(float(v) for v in pose.pos), *(float(v) for v in pose.quat)))
        for rows in self._objects.values():
            if len(rows) == idx:  # object absent this step: NaN-pad to stay step-aligned
                rows.append(_NAN_POSE)

    # -- event capture ------------------------------------------------------------

    def on_event(self, kind: str, payload: dict[str, Any] | None = None) -> None:
        """Append one event to events.jsonl immediately (streamed, crash-durable).

        ``kind`` must be one of ``EVENT_KINDS``. Each line carries ``t`` (wall clock,
        epoch seconds) and, when the payload provides it, ``sim_t`` promoted to the
        top level; the remaining payload nests under ``payload``.
        """
        self._ensure_open("on_event")
        if kind not in EVENT_KINDS:
            raise ValueError(f"unknown event kind {kind!r}; allowed: {sorted(EVENT_KINDS)}")
        body = dict(payload or {})
        record: dict[str, Any] = {"t": time.time(), "kind": kind}
        if "sim_t" in body:
            record["sim_t"] = body.pop("sim_t")
        record["payload"] = body
        # Open-append-close per event: low rate, and every event survives a later crash.
        with open(self.path / "events.jsonl", "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    # -- finalization -------------------------------------------------------------

    def finish(self, success: bool, detail: str = "") -> Path:
        """Write steps.npz + final meta.json and close the writer; returns the episode dir."""
        self._ensure_open("finish")
        return self._finalize(success=bool(success), detail=detail, aborted=False)

    def abort(self, reason: str = "") -> Path:
        """Crash path: finalize with success=False, keeping every buffered step.

        Safe to call unconditionally from ``except``/``finally`` — a no-op returning
        the episode dir when the writer is already closed.
        """
        if self._closed:
            return self.path
        return self._finalize(success=False, detail=reason, aborted=True)

    def _finalize(self, *, success: bool, detail: str, aborted: bool) -> Path:
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


# -- reading back ------------------------------------------------------------------


@dataclass(eq=False)  # eq=False: ndarray fields make generated __eq__ ambiguous
class Episode:
    """One recorded episode loaded back into memory (replay / tests / eval)."""

    path: Path
    meta: dict[str, Any]
    timestamp: np.ndarray  # (n,) float32, embodiment clock seconds
    state: np.ndarray  # (n, dof+1) float32 — stored as "observation.state"
    action: np.ndarray  # (n, dof+1) float32
    ee_pos: np.ndarray  # (n, 3) float32
    objects: dict[str, np.ndarray]  # name -> (n, 7) float32 pos+quat, NaN where absent
    events: list[dict[str, Any]]

    def __len__(self) -> int:
        return int(self.timestamp.shape[0])


def load_episode(path: Path | str) -> Episode:
    """Load one episode directory written by EpisodeWriter."""
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


# -- LeRobot conversion (M2) -------------------------------------------------------


def to_lerobot(episodes_root: Path | str, out_dir: Path | str) -> None:
    """Convert v0 episodes under ``episodes_root`` into a LeRobotDataset at ``out_dir``.

    NOT IMPLEMENTED in v0: the heavy ``lerobot``/``torch`` dependencies are deferred
    to M2 (docs/decisions.md D012; the format-alignment decision itself is D003).
    Field mapping the converter will apply (v0 name -> LeRobotDataset v3 name):

    ==================  ==========================
    v0 (this module)    LeRobotDataset v3
    ==================  ==========================
    observation.state   observation.state
    action              action
    timestamp           timestamp
    meta.task           tasks (task index table)
    ==================  ==========================

    ``ee_pos`` and ``object/<name>`` arrays plus the rest of meta.json travel in the
    versioned episode-metadata extension layer (D003), not in core LeRobot columns.
    """
    raise NotImplementedError(
        "to_lerobot lands in M2 with the lerobot/torch deps (docs/decisions.md D012). "
        f"v0 episodes under {episodes_root!s} already use LeRobot-aligned field names "
        f"(see LEROBOT_FIELD_MAP), so converting later into {out_dir!s} loses nothing."
    )
