"""Blocking motion primitives over the Embodiment protocol.

Joint-space interpolation with guard-checked writes; a single denied write aborts
the whole motion (the guard latches on breach — motions never fight it). These run
synchronously and are wrapped in asyncio.to_thread by the skill handlers.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from embodied.control.hal import ActionCommand


def _phys_per_write(emb: Any, hz: float) -> int:
    return max(1, round((1.0 / hz) / emb.timestep))


def move_to_q(
    emb: Any,
    q_target: np.ndarray,
    *,
    seconds: float = 1.2,
    hz: float = 50.0,
    settle_steps: int = 60,
    gripper: float | None = None,
) -> bool:
    q0 = np.asarray(emb.read().qpos, dtype=float)
    qt = np.asarray(q_target, dtype=float)
    steps = max(2, int(seconds * hz))
    per = _phys_per_write(emb, hz)
    for k in range(1, steps + 1):
        q = q0 + (qt - q0) * (k / steps)
        result = emb.write(ActionCommand(joint_targets=tuple(q), gripper=gripper))
        if not result.applied:
            return False
        emb.step(per)
    emb.step(settle_steps)
    return True


def set_gripper(emb: Any, opening: float, *, seconds: float = 0.5, hz: float = 50.0, settle_steps: int = 100) -> bool:
    hold = tuple(emb.read().qpos)
    steps = max(2, int(seconds * hz))
    per = _phys_per_write(emb, hz)
    start = emb.read().gripper_opening
    for k in range(1, steps + 1):
        g = start + (opening - start) * (k / steps)
        result = emb.write(ActionCommand(joint_targets=hold, gripper=g))
        if not result.applied:
            return False
        emb.step(per)
    emb.step(settle_steps)
    return True


def reach(
    emb: Any,
    target_pos: Any,
    *,
    approach_down: bool = True,
    max_pos_err: float = 0.006,
    seconds: float = 1.2,
    gripper: float | None = None,
) -> bool:
    """IK to a cartesian grasp-site target, then joint-space move. Fails closed:
    unreachable target (per tolerance) or fence-vetoed target → no motion at all."""
    target = np.asarray(target_pos, dtype=float)
    if hasattr(emb, "guard") and not emb.guard.check_target(tuple(target)):
        return False
    res = emb.solve_reach(target)
    if res.pos_err > max_pos_err:
        return False
    return move_to_q(emb, res.qpos, seconds=seconds, gripper=gripper)
