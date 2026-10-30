"""MuJoCo-only hold and stop observation. 仅用于 MuJoCo 的保持与停止观测。"""

from __future__ import annotations

import hashlib
import math
import time
from functools import lru_cache
from pathlib import Path

import numpy as np

from embodied.contracts.stopping import (
    StopEvidence,
    StopProfile,
    StopRejected,
    StopRequest,
    StopSample,
    validate_stop_evidence,
)
from embodied.control.hal import ActionCommand


@lru_cache(maxsize=8)
def _asset_digest(scene: str) -> str:
    root = Path(scene).parent
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


class SimStopController:
    def __init__(self, sim):
        import mujoco

        self.sim = sim
        spec = sim.spec()
        joints = (*spec.joints, spec.gripper_joint)
        self.profile = StopProfile(
            robot_id=spec.embodiment_id, asset_digest=_asset_digest(str(sim._scene_path)),
            joint_names=tuple(j.name for j in joints), joint_lower=tuple(j.lower for j in joints),
            joint_upper=tuple(j.upper for j in joints),
        )
        self._owner: str | None = None
        self._issued: StopEvidence | None = None
        self._jaw_bodies = tuple(sim._name2id(mujoco.mjtObj.mjOBJ_BODY, name) for name in ("Fixed_Jaw", "Moving_Jaw"))
        self._object_bodies = {body: name for name, body in sim._objects.items()}

    def _contacts(self) -> dict[str, tuple[float, float]]:
        import mujoco

        sim = self.sim
        forces: dict[str, list[float]] = {}
        for index, contact in enumerate(sim.data.contact):
            bodies = [int(sim.model.geom_bodyid[geom]) for geom in (contact.geom1, contact.geom2)]
            for side in (0, 1):
                obj = self._object_bodies.get(bodies[side])
                if obj is None:
                    continue
                body = bodies[1 - side]
                while body and body not in self._jaw_bodies:
                    body = int(sim.model.body_parentid[body])
                if body in self._jaw_bodies:
                    force = np.zeros(6)
                    mujoco.mj_contactForce(sim.model, sim.data, index, force)
                    if not np.isfinite(force).all() or force[0] < 0:
                        raise StopRejected("invalid_contact_feedback")
                    values = forces.setdefault(obj, [0., 0.])
                    values[self._jaw_bodies.index(body)] += float(force[0])
        return {name: tuple(values) for name, values in forces.items()}

    def _sample(self, contact_objects: tuple[str, ...] = ()) -> StopSample:
        sim = self.sim
        if (not all(np.isfinite(values).all() for values in (sim.data.qpos, sim.data.qvel, sim.data.ctrl))
                or any(warning.number for warning in sim.data.warning)):
            raise StopRejected("invalid_simulation_feedback")
        contacts = self._contacts() if contact_objects else {}
        return StopSample(
            sim_t=float(sim.data.time),
            positions=(*tuple(float(v) for v in sim.data.qpos[sim._qpos_idx]),
                       float(sim.data.qpos[sim._jaw_qpos_adr])),
            velocities=(*tuple(float(v) for v in sim.data.qvel[sim._dof_idx]),
                        float(sim.data.qvel[sim._jaw_dof_adr])),
            grip_forces=tuple(force for obj in contact_objects for force in contacts.get(obj, (0., 0.))),
        )

    def prepare(self, request: StopRequest) -> StopEvidence:
        import mujoco

        profile, sim = self.profile, self.sim
        if request.robot_id != profile.robot_id or set(request.resources) != set(profile.resources):
            raise StopRejected("stop_controller_binding_mismatch")
        started = time.monotonic()
        remaining = request.deadline_monotonic - started
        if remaining <= 0:
            raise StopRejected("stop_request_expired")
        if not sim._state_lock.acquire(timeout=min(remaining, profile.max_wall_duration_s)):
            raise StopRejected("stop_controller_busy")
        try:
            if time.monotonic() >= request.deadline_monotonic:
                raise StopRejected("stop_request_expired")
            if sim._stop_fence and self._owner != request.operation_id:
                raise StopRejected("other_stop_reconciliation_pending")
            sim._stop_fence = request.nonce
            self._owner = request.operation_id
            self._issued = None
            initial = self._sample()
            targets = tuple(float(v) for v in np.clip(initial.positions, profile.joint_lower, profile.joint_upper))
            if any(abs(a - b) > profile.soft_limit_tolerance_rad for a, b in zip(initial.positions, targets)):
                raise StopRejected("stop_feedback_out_of_range")
            previous_gripper = float(sim.data.ctrl[sim._jaw_act])
            contact_objects = ()
            if profile.joint_lower[-1] <= previous_gripper <= min(initial.positions[-1], profile.joint_upper[-1]):
                contact_objects = tuple(sorted(name for name, forces in self._contacts().items()
                                               if min(forces) >= profile.min_contact_force_n))
            strategy = "maintain_contact" if contact_objects else "hold_position"
            if contact_objects:
                # 双侧接触时保留已接受的闭合目标，避免卸掉夹持力。 / Preserve the accepted closing target under bilateral contact.
                targets = (*targets[:-1], previous_gripper)
            dt = sim.timestep
            if not math.isfinite(dt) or dt <= 0:
                raise StopRejected("invalid_simulation_clock")
            # 保持机械臂姿态，夹爪按接触策略处理，不清零速度。 / Hold arm posture with contact-aware grip targets; never zero velocity.
            sim.data.ctrl[sim._act_ids] = targets[:-1]
            sim.data.ctrl[sim._jaw_act] = targets[-1]
            sim._last_cmd = ActionCommand(joint_targets=targets[:-1],
                                          gripper=targets[-1] / profile.joint_upper[-1], source="maintenance_stop")
            window: list[StopSample] = []
            for _ in range(math.ceil(profile.max_sim_duration_s / dt)):
                if time.monotonic() > request.deadline_monotonic:
                    raise StopRejected("stop_observation_timeout")
                mujoco.mj_step(sim.model, sim.data)
                sample = self._sample(contact_objects)
                if (all(abs(v) <= profile.max_velocity_rad_s for v in sample.velocities)
                        and all(force >= profile.min_contact_force_n for force in sample.grip_forces)):
                    window.append(sample)
                else:
                    window.clear()
                if len(window) >= 2 and window[-1].sim_t - window[0].sim_t + 1e-9 >= profile.stable_duration_s:
                    evidence = StopEvidence(
                        **request.model_dump(), profile_digest=profile.digest, scene_epoch=sim.state_epoch,
                        observed_at_unix_s=time.time(), measured_at_monotonic=time.monotonic(),
                        hold_targets=targets, samples=tuple(window),
                        initial_positions=initial.positions, previous_gripper_target=previous_gripper,
                        gripper_strategy=strategy, contact_objects=contact_objects,
                    )
                    validate_stop_evidence(evidence, request, profile)
                    self._issued = evidence
                    return evidence
            raise StopRejected("grip_contact_not_preserved" if contact_objects else "motion_not_stopped")
        finally:
            sim._state_lock.release()

    def commit(self, evidence: StopEvidence, request: StopRequest, persist):
        """Persist and release while ordinary mutations remain fenced.

        普通状态修改仍被围栏阻断时，先落账再释放。
        """
        with self.sim._state_lock:
            if self._issued is not evidence or self.sim._stop_fence != evidence.nonce:
                raise StopRejected("stop_evidence_not_issued_here")
            if self.sim.state_epoch != evidence.scene_epoch:
                raise StopRejected("stop_scene_changed")
            validate_stop_evidence(evidence, request, self.profile)
            result = persist()
            self.sim._stop_fence = None
            self._owner = None
            self._issued = None
            return result
