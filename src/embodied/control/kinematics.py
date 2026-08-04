"""Differential IK (damped least squares) on MuJoCo jacobians. No extra dependencies.

Position task (3 dof) plus a soft axis-alignment task that points the gripper's
approach axis at a world direction (default: straight down for top grasps) — the
right shape for a 5-DOF arm where full 6D pose tracking is over-constrained.
Operates on a scratch MjData so the live simulation state is never disturbed.
"""

from __future__ import annotations

from dataclasses import dataclass

import mujoco
import numpy as np


@dataclass
class IKResult:
    qpos: np.ndarray  # solution for the controlled joints, input order
    pos_err: float  # meters
    axis_err: float  # radians between approach axis and target axis
    converged: bool
    iters: int


def solve_ik(
    model: mujoco.MjModel,
    site_name: str,
    target_pos: np.ndarray,
    q_init: np.ndarray,
    qpos_idx: np.ndarray,
    dof_idx: np.ndarray,
    *,
    base_qpos: np.ndarray | None = None,
    approach_axis_local: tuple[float, float, float] = (0.0, -1.0, 0.0),
    target_axis_world: tuple[float, float, float] | None = (0.0, 0.0, -1.0),
    ori_weight: float = 0.15,
    joint_lower: np.ndarray | None = None,
    joint_upper: np.ndarray | None = None,
    iters: int = 300,
    damping: float = 0.02,
    pos_tol: float = 2.5e-3,
    step_clip: float = 0.2,
    axis_tol: float = 0.25,
) -> IKResult:
    site_id = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_SITE, site_name)
    if site_id < 0:
        raise ValueError(f"site {site_name!r} not in model")
    scratch = mujoco.MjData(model)
    if base_qpos is not None:
        scratch.qpos[:] = base_qpos
    q = np.asarray(q_init, dtype=float).copy()
    a_local = np.asarray(approach_axis_local, dtype=float)
    t_axis = None if target_axis_world is None else np.asarray(target_axis_world, dtype=float)
    target = np.asarray(target_pos, dtype=float)

    pos_err = axis_err = float("inf")
    it = 0
    for it in range(1, iters + 1):
        scratch.qpos[qpos_idx] = q
        mujoco.mj_forward(model, scratch)
        pos = scratch.site_xpos[site_id]
        rot = scratch.site_xmat[site_id].reshape(3, 3)
        e_pos = target - pos
        pos_err = float(np.linalg.norm(e_pos))
        if t_axis is not None:
            a_world = rot @ a_local
            e_ori = np.cross(a_world, t_axis)  # rotates approach axis toward target axis
            axis_err = float(np.arcsin(np.clip(np.linalg.norm(e_ori), 0.0, 1.0)))
            if float(a_world @ t_axis) < 0:  # anti-parallel: arcsin blind spot
                axis_err = float(np.pi) - axis_err
        else:
            e_ori = np.zeros(3)
            axis_err = 0.0
        if pos_err < pos_tol and axis_err < axis_tol:
            return IKResult(qpos=q, pos_err=pos_err, axis_err=axis_err, converged=True, iters=it)

        jacp = np.zeros((3, model.nv))
        jacr = np.zeros((3, model.nv))
        mujoco.mj_jacSite(model, scratch, jacp, jacr, site_id)
        jac = np.vstack([jacp[:, dof_idx], ori_weight * jacr[:, dof_idx]])
        err = np.concatenate([e_pos, ori_weight * e_ori])
        dq = jac.T @ np.linalg.solve(jac @ jac.T + damping * np.eye(6), err)
        q = q + np.clip(dq, -step_clip, step_clip)
        if joint_lower is not None:
            q = np.maximum(q, joint_lower)
        if joint_upper is not None:
            q = np.minimum(q, joint_upper)

    return IKResult(qpos=q, pos_err=pos_err, axis_err=axis_err, converged=False, iters=it)
