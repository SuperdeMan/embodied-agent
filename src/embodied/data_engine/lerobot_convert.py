"""v0 episode directories -> LeRobotDataset v3 (docs/decisions.md D003, D012, D014).

The heavy ``lerobot``/``torch`` imports happen inside :func:`convert_episodes` so this
module stays importable everywhere (CI has no learn group); callers get a clear
actionable error instead of an ImportError at the top of the file.

Conversion contract (D014):
- Core columns: ``observation.state``, ``action`` (both dof+gripper), plus
  ``observation.environment_state`` = object poses (pos3+quat4 each) concatenated in
  sorted-name order — the perception-replacement seam: perception v1 will fill the
  same vector that sim ground truth fills today.
- v0 capture cadence is irregular (50 Hz command spacing, one sample per settle
  call), so every segment is resampled onto a uniform ``fps`` grid: zero-order hold
  for actions (position targets are step signals), linear interpolation for
  observation channels. Frame timestamps are then exactly ``k/fps`` (LeRobot's
  tolerance contract holds by construction).
- ``segment="episode"`` emits one dataset episode per recorded episode, task = the
  natural-language command. ``segment="skill"`` splits at the sim_t boundaries that
  ``embodied collect`` wrote into events.jsonl, task = skill name — the data basis
  for replacing scripted skills one at a time (manifest unchanged).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

import numpy as np

from embodied.data_engine.recorder import Episode, load_episode

_POSE_SUFFIXES = ("x", "y", "z", "qw", "qx", "qy", "qz")


@dataclass
class ConvertReport:
    out_dir: Path
    fps: int
    episodes_converted: int = 0
    segments_written: int = 0
    frames_written: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (episode dir, reason)


@dataclass(frozen=True)
class _Segment:
    task: str
    t0: float
    t1: float


def _monotonic(ts: np.ndarray) -> np.ndarray:
    """Mask keeping only strictly-increasing timestamps (guards interp/searchsorted)."""
    return np.diff(ts, prepend=-np.inf) > 0


def _zoh(ts: np.ndarray, ys: np.ndarray, grid: np.ndarray) -> np.ndarray:
    idx = np.clip(np.searchsorted(ts, grid, side="right") - 1, 0, len(ts) - 1)
    return ys[idx]


def _lerp(ts: np.ndarray, ys: np.ndarray, grid: np.ndarray) -> np.ndarray:
    return np.stack([np.interp(grid, ts, ys[:, j]) for j in range(ys.shape[1])], axis=1)


def _fill_nan_columns(arr: np.ndarray) -> np.ndarray:
    """Forward- then back-fill NaN rows per column (objects absent for a few steps).

    Returns a copy; raises if any column is entirely NaN (object never observed —
    that episode cannot supply a dense environment_state).
    """
    out = arr.copy()
    for j in range(out.shape[1]):
        col = out[:, j]
        nan = np.isnan(col)
        if nan.all():
            raise ValueError("environment_state column entirely NaN")
        if not nan.any():
            continue
        idx = np.arange(len(col))
        col[nan] = np.interp(idx[nan], idx[~nan], col[~nan])  # interior lerp, edges clamp
    return out


def _env_state(ep: Episode, names_order: list[str]) -> np.ndarray:
    return np.concatenate([_fill_nan_columns(ep.objects[n]) for n in names_order], axis=1)


def _skill_segments(ep: Episode) -> list[_Segment]:
    """Pair skill_start/skill_end events (top-level sim_t) into time segments."""
    segments: list[_Segment] = []
    open_start: tuple[str, float] | None = None
    for ev in ep.events:
        kind, sim_t = ev.get("kind"), ev.get("sim_t")
        if kind not in ("skill_start", "skill_end") or sim_t is None:
            continue
        skill = str(ev.get("payload", {}).get("skill", ""))
        if kind == "skill_start":
            open_start = (skill, float(sim_t))
        elif open_start is not None and open_start[0] == skill:
            segments.append(_Segment(task=skill, t0=open_start[1], t1=float(sim_t)))
            open_start = None
    if open_start is not None and len(ep.timestamp):  # crash mid-skill: close at last sample
        segments.append(_Segment(task=open_start[0], t0=open_start[1], t1=float(ep.timestamp[-1])))
    return segments


def _feature(dim: int, names: list[str] | None) -> dict[str, Any]:
    return {"dtype": "float32", "shape": (dim,), "names": names}


def _state_names(meta: dict[str, Any], dim: int) -> list[str]:
    names = (meta.get("extra_meta") or {}).get("state_names")
    if isinstance(names, list) and len(names) == dim:
        return [str(n) for n in names]
    return [f"motor_{i}" for i in range(dim - 1)] + ["gripper"]


def convert_episodes(
    episodes_root: Path | str,
    out_dir: Path | str,
    *,
    fps: int = 50,
    segment: Literal["episode", "skill"] = "episode",
    skills: tuple[str, ...] | None = None,
    include_failures: bool = False,
    repo_id: str | None = None,
    progress: Callable[[str], None] = print,
) -> ConvertReport:
    """Convert every v0 episode under ``episodes_root`` into one LeRobotDataset.

    Episodes with success=False (or aborted) are excluded unless
    ``include_failures`` — failures stay valuable on disk, but the default output
    is an imitation-learning demo set. ``out_dir`` must not exist yet: datasets are
    immutable artifacts, re-convert to a fresh directory instead of appending.
    ``skills`` (skill mode only) keeps just the named skills' segments — vanilla ACT
    is not task-conditioned, so per-skill policies train on per-skill datasets.
    """
    episodes_root, out_dir = Path(episodes_root), Path(out_dir)
    if out_dir.exists():
        raise FileExistsError(f"{out_dir} already exists — datasets are immutable, convert to a fresh directory")
    candidates = sorted(p for p in episodes_root.iterdir() if (p / "meta.json").is_file())
    if not candidates:
        raise FileNotFoundError(f"no episodes under {episodes_root}")

    report = ConvertReport(out_dir=out_dir, fps=fps)

    # Pass 1 — prepare in memory: filter, resample, segment. The dataset directory is
    # only created once at least one segment is known-good (no half-written artifacts).
    prepared: list[tuple[str, list[tuple[_Segment, dict[str, np.ndarray]]]]] = []
    first_meta: dict[str, Any] = {}
    state_dim = -1
    env_names: list[str] = []
    for ep_dir in candidates:
        meta = json.loads((ep_dir / "meta.json").read_text(encoding="utf-8"))
        if not include_failures and meta.get("success") is not True:
            report.skipped.append((ep_dir.name, f"success={meta.get('success')}"))
            continue
        if not (ep_dir / "steps.npz").is_file():
            report.skipped.append((ep_dir.name, "no steps.npz (hard-killed episode)"))
            continue
        ep = load_episode(ep_dir)
        keep = _monotonic(ep.timestamp)
        ts = ep.timestamp[keep].astype(np.float64)
        if len(ts) < 2:
            report.skipped.append((ep_dir.name, f"too short ({len(ts)} monotonic samples)"))
            continue

        if state_dim < 0:
            state_dim, env_names, first_meta = ep.state.shape[1], sorted(ep.objects), meta
        elif ep.state.shape[1] != state_dim or sorted(ep.objects) != env_names:
            raise ValueError(
                f"{ep_dir.name}: layout mismatch (state dim {ep.state.shape[1]} vs {state_dim}, "
                f"objects {sorted(ep.objects)} vs {env_names}) — mixed episode roots?"
            )

        state, action = ep.state[keep], ep.action[keep]
        env = _env_state(ep, env_names)[keep] if env_names else None

        if segment == "skill":
            segments = _skill_segments(ep)
            if not segments:
                report.skipped.append((ep_dir.name, "no sim_t skill boundaries (pre-M2 recording)"))
                continue
            if skills:
                segments = [s for s in segments if s.task in skills]
                if not segments:
                    report.skipped.append((ep_dir.name, f"no segments matching {sorted(skills)}"))
                    continue
        else:
            segments = [_Segment(task=str(meta.get("task", "")), t0=float(ts[0]), t1=float(ts[-1]))]

        good: list[tuple[_Segment, dict[str, np.ndarray]]] = []
        for seg in segments:
            n_frames = int(np.floor((seg.t1 - seg.t0) * fps)) + 1
            if n_frames < 2:
                report.skipped.append((ep_dir.name, f"segment {seg.task!r} shorter than 2/{fps}s"))
                continue
            grid = seg.t0 + np.arange(n_frames, dtype=np.float64) / fps
            arrays = {
                "observation.state": _lerp(ts, state, grid).astype(np.float32),
                "action": _zoh(ts, action, grid).astype(np.float32),
            }
            if env is not None:
                arrays["observation.environment_state"] = _lerp(ts, env, grid).astype(np.float32)
            good.append((seg, arrays))
        if good:
            prepared.append((ep_dir.name, good))

    if not prepared:
        raise ValueError(
            f"no convertible episodes under {episodes_root} "
            f"(skipped: {report.skipped or 'none'}) — record with `embodied collect` first"
        )

    # Pass 2 — write the dataset. The heavy import happens only once there is real
    # work to do; path/filter errors above never require the learn group installed.
    try:
        from lerobot.datasets.lerobot_dataset import LeRobotDataset
    except ImportError as e:  # pragma: no cover - exercised only without the learn group
        raise RuntimeError(
            "lerobot is required for conversion: uv sync --group learn (docs/decisions.md D014)"
        ) from e

    names = _state_names(first_meta, state_dim)
    features = {"observation.state": _feature(state_dim, names), "action": _feature(state_dim, names)}
    if env_names:
        features["observation.environment_state"] = _feature(
            7 * len(env_names), [f"{n}.{s}" for n in env_names for s in _POSE_SUFFIXES]
        )
    dataset = LeRobotDataset.create(
        repo_id=repo_id or f"local/{out_dir.name}",
        fps=fps,
        features=features,
        root=out_dir,
        robot_type=str(first_meta.get("embodiment_id", "")) or None,
        use_videos=False,
    )
    for ep_name, good in prepared:
        for seg, arrays in good:
            n_frames = len(arrays["action"])
            for k in range(n_frames):
                frame: dict[str, Any] = {key: np.ascontiguousarray(arr[k]) for key, arr in arrays.items()}
                frame["task"] = seg.task
                dataset.add_frame(frame)
            dataset.save_episode()
            report.segments_written += 1
            report.frames_written += n_frames
        report.episodes_converted += 1
        progress(f"[convert] {ep_name}: {len(good)} segment(s)")
    dataset.finalize()
    progress(
        f"[convert] wrote {report.segments_written} episode(s), {report.frames_written} frames "
        f"@ {fps} fps -> {out_dir} (skipped {len(report.skipped)})"
    )
    return report
