"""Teleoperation channel v0: incremental commands -> IK -> guarded writes -> episodes.

The session is INPUT-AGNOSTIC: jog/gripper/episode-mark commands in, guarded motion
plus recording out. The terminal keyboard frontend lives in the CLI; a gamepad or
the real leader arm (M3) will drive this same path — that seam is the point of the
roadmap's teleop task. Recording is default ON (architecture §4.6: every run is
data); episodes carry ``extra_meta.collector = "teleop/v1"`` so datasets can keep
human demos separate from scripted-expert ones (D014 conversion works unchanged).

Safety: every motion goes through the same guarded write path as skills — the
fence pre-check refuses out-of-bounds jog targets BEFORE any motion, and a guard
veto mid-motion aborts the jog (never fought, same rule as motions.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from embodied.data_engine.recorder import EpisodeRecorder, EpisodeWriter
from embodied.skills.scripted import motions

TELEOP_COLLECTOR_ID = "teleop/v1"

GRIP_OPEN = 0.6  # matches the scripted skills' grasp opening: consistent demo data
GRIP_CLOSED = 0.0


@dataclass
class JogResult:
    ok: bool
    reason: str = ""  # "fence" | "unreachable" | "denied" | ""


class TeleopSession:
    """One operator session over a sim embodiment; episodes open/close explicitly."""

    def __init__(
        self,
        sim: Any,
        *,
        task: str,
        root: Path | str | None = None,
        jog_seconds: float = 0.12,
        max_pos_err: float = 0.008,
        record: bool = True,
    ) -> None:
        self.sim = sim
        self.task = task
        self.jog_seconds = jog_seconds
        self.max_pos_err = max_pos_err
        self.recorder = EpisodeRecorder(root) if record else None
        self.writer: EpisodeWriter | None = None
        self.episodes = 0
        spec = sim.spec()
        self._state_names = [j.name for j in spec.joints]
        if self._state_names and spec.gripper_joint is not None:
            self._state_names.append(spec.gripper_joint.name)

    # -- episode lifecycle --------------------------------------------------------

    def start_episode(self, *, randomize: bool = True) -> None:
        """Reset the scene and open a fresh episode (closing any dangling one as failed)."""
        if self.writer is not None:
            self.abort_episode("superseded by new episode")
        self.sim.reset(randomize=randomize)
        if self.recorder is not None:
            extra: dict[str, Any] = {"collector": TELEOP_COLLECTOR_ID, "episode_index": self.episodes}
            if self._state_names:
                extra["state_names"] = self._state_names
            self.writer = self.recorder.start(
                task=self.task, embodiment_id=self.sim.spec().embodiment_id, extra_meta=extra
            )
            self.sim.set_hooks(
                on_step=self.writer.on_step,
                on_guard_event=lambda e, w=self.writer: w.on_event("safety", {"kind": e.kind, "reason": e.reason}),
            )
        self.episodes += 1

    def finish_episode(self, success: bool) -> Path | None:
        """Human marks the outcome — the operator IS the judge in teleop."""
        if self.writer is None:
            return None
        self.sim.set_hooks(None, None)
        path = self.writer.finish(success, detail="operator-marked")
        self.writer = None
        return path

    def abort_episode(self, reason: str = "operator abort") -> None:
        if self.writer is None:
            return
        self.sim.set_hooks(None, None)
        self.writer.abort(reason)
        self.writer = None

    def close(self) -> None:
        """End of session: a dangling episode is kept, labeled failed (data, not garbage)."""
        self.abort_episode("session closed mid-episode")

    # -- motion commands ----------------------------------------------------------

    def jog(self, dx: float, dy: float, dz: float) -> JogResult:
        """Cartesian nudge of the grasp point, top-down orientation held.

        Fails closed without any motion when the target leaves the safety fence or
        IK cannot reach it within tolerance.
        """
        obs = self.sim.read()
        target = np.asarray(obs.ee_pose.pos, dtype=float) + [dx, dy, dz]
        if hasattr(self.sim, "guard") and not self.sim.guard.check_target(tuple(target)):
            return JogResult(ok=False, reason="fence")
        res = self.sim.solve_reach(target)
        if res.pos_err > self.max_pos_err:
            return JogResult(ok=False, reason="unreachable")
        if not motions.move_to_q(self.sim, res.qpos, seconds=self.jog_seconds, settle_steps=10):
            return JogResult(ok=False, reason="denied")
        return JogResult(ok=True)

    def set_gripper(self, opening: float) -> bool:
        return motions.set_gripper(self.sim, float(np.clip(opening, 0.0, 1.0)), seconds=0.3, settle_steps=30)

    def toggle_gripper(self) -> bool:
        current = self.sim.read().gripper_opening
        return self.set_gripper(GRIP_CLOSED if current > 0.3 else GRIP_OPEN)

    def home(self) -> bool:
        return motions.move_to_q(self.sim, self.sim.home_qpos(), seconds=1.0)

    # -- introspection ------------------------------------------------------------

    def status(self) -> str:
        obs = self.sim.read()
        x, y, z = obs.ee_pose.pos
        rec = "REC" if self.writer is not None else "---"
        return f"[{rec}] ep{self.episodes:03d} ee=({x:+.3f},{y:+.3f},{z:+.3f}) grip={obs.gripper_opening:.2f}"
