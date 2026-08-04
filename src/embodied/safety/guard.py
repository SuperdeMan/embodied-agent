"""SG-2 v0: deterministic guard on the HAL write path (docs/architecture.md §4.5).

Runs in-process inside the driver for M1 (the standalone guardian process — proto
already defined — arrives with the process split). Nothing here may ever depend on
an AI output being correct: pure geometry, limits, and whitelists. A fence breach
LATCHES the halted state; only an explicit reset() releases it, so a runaway
trajectory cannot "recover on its own" and keep moving.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from embodied.control.hal import ActionCommand, Observation, WriteResult

Vec3 = tuple[float, float, float]


@dataclass(frozen=True)
class GuardLimits:
    joint_lower: tuple[float, ...]
    joint_upper: tuple[float, ...]
    max_delta_per_write: float = 0.12  # rad, per joint per write (position-servo lunge backstop)
    ee_fence_min: Vec3 = (-0.45, -0.45, -0.005)
    ee_fence_max: Vec3 = (0.45, 0.45, 0.60)
    allowed_sources: frozenset[str] = frozenset({"skill", "teleop", "reset"})


@dataclass
class GuardEvent:
    kind: str  # denied | clamped | halted | estop | reset
    reason: str
    detail: dict = field(default_factory=dict)


class Guard:
    def __init__(self, limits: GuardLimits, on_event: Callable[[GuardEvent], None] | None = None):
        self.limits = limits
        self._on_event = on_event
        self.halted = False
        self.halt_reason = ""

    # -- lifecycle -------------------------------------------------------------

    def estop(self, reason: str = "estop") -> None:
        self._latch(reason, kind="estop")

    def set_listener(self, on_event: Callable[[GuardEvent], None] | None) -> None:
        self._on_event = on_event

    def reset(self) -> None:
        self.halted = False
        self.halt_reason = ""
        self._emit(GuardEvent(kind="reset", reason="guard reset"))

    # -- write-path gate -------------------------------------------------------

    def gate(self, cmd: ActionCommand, obs: Observation) -> tuple[ActionCommand | None, WriteResult]:
        """Validate/clamp a command against the current observation.

        Returns (command_to_apply, result); command is None when denied.
        """
        lim = self.limits
        if self.halted:
            return None, self._deny(f"halted:{self.halt_reason}")
        if cmd.source not in lim.allowed_sources:
            return None, self._deny(f"source_not_whitelisted:{cmd.source}")
        if len(cmd.joint_targets) != len(lim.joint_lower):
            return None, self._deny(f"bad_command_shape:{len(cmd.joint_targets)}")

        # Fence check on the CURRENT end-effector: breach latches halt.
        px, py, pz = obs.ee_pose.pos
        fmin, fmax = lim.ee_fence_min, lim.ee_fence_max
        if not (fmin[0] <= px <= fmax[0] and fmin[1] <= py <= fmax[1] and fmin[2] <= pz <= fmax[2]):
            self._latch(f"ee_fence_breach:({px:.3f},{py:.3f},{pz:.3f})", kind="halted")
            return None, self._deny("ee_fence_breach")

        clamped = False
        targets = []
        for i, t in enumerate(cmd.joint_targets):
            t2 = min(max(t, lim.joint_lower[i]), lim.joint_upper[i])
            cur = obs.qpos[i]
            lo, hi = cur - lim.max_delta_per_write, cur + lim.max_delta_per_write
            t3 = min(max(t2, lo), hi)
            clamped = clamped or (t3 != t)
            targets.append(t3)
        gripper = cmd.gripper
        if gripper is not None:
            g2 = min(max(gripper, 0.0), 1.0)
            clamped = clamped or (g2 != gripper)
            gripper = g2
        if clamped:
            self._emit(GuardEvent(kind="clamped", reason="range_or_rate", detail={"source": cmd.source}))
        out = ActionCommand(joint_targets=tuple(targets), gripper=gripper, source=cmd.source)
        return out, WriteResult(applied=True, clamped=clamped)

    def check_target(self, pos: Vec3) -> bool:
        """Advisory pre-check for planned cartesian targets (skills call this before moving)."""
        fmin, fmax = self.limits.ee_fence_min, self.limits.ee_fence_max
        return all(fmin[i] <= pos[i] <= fmax[i] for i in range(3))

    # -- internals -------------------------------------------------------------

    def _latch(self, reason: str, kind: str) -> None:
        self.halted = True
        self.halt_reason = reason
        self._emit(GuardEvent(kind=kind, reason=reason))

    def _deny(self, reason: str) -> WriteResult:
        self._emit(GuardEvent(kind="denied", reason=reason))
        return WriteResult(applied=False, reason=reason)

    def _emit(self, event: GuardEvent) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event)
        except Exception:
            pass  # guard never fails because a listener did
