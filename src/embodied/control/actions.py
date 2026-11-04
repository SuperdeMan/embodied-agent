"""Receiver-side action authority for one simulated body. 单个仿真本体的接收端动作权威。"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import asdict
from uuid import uuid4

from embodied.contracts.action import ActionEnvelope, ActionProfile, ControlObservationRef
from embodied.control.hal import ActionCommand, WriteResult
from embodied.runtime.invocation import CURRENT_INVOCATION


class ActionRejected(RuntimeError):
    pass


class ActionAuthority:
    def __init__(self, sim):
        self.sim = sim
        self.runtime = None
        self.boot_id = uuid4().hex
        self.epoch = uuid4().hex
        self._clock = time.monotonic
        self._limits = sim.guard.limits
        limits = asdict(self._limits)
        limits["allowed_sources"] = sorted(limits["allowed_sources"])
        self.profile = ActionProfile(
            robot_id=sim.spec().embodiment_id, joint_names=tuple(j.name for j in sim.spec().joints),
            calibration_id=sim.stop_controller.profile.asset_digest,
            limits_digest=hashlib.sha256(json.dumps(limits, sort_keys=True).encode()).hexdigest(),
        )
        self._references: OrderedDict[str, ControlObservationRef] = OrderedDict()
        self._read_sequence = 0

    @contextmanager
    def admission(self):
        with self.sim._state_lock:
            yield

    def bind(self, runtime) -> None:
        with self.admission():
            if self.runtime is not None and self.runtime is not runtime:
                raise ActionRejected("controller_already_bound")
            if self.runtime is None:
                self.runtime = runtime
                self.invalidate()

    def invalidate(self) -> None:
        self.epoch = uuid4().hex
        self._references.clear()

    def observe(self, sim_t: float) -> ControlObservationRef:
        self._read_sequence += 1
        ref = ControlObservationRef(
            reference_id=uuid4().hex, robot_id=self.profile.robot_id, controller_boot_id=self.boot_id,
            control_epoch=self.epoch, scene_epoch=self.sim.state_epoch, profile_digest=self.profile.digest,
            sequence=self._read_sequence, captured_monotonic=self._clock(), sim_t=sim_t,
        )
        self._references[ref.reference_id] = ref
        while len(self._references) > 4096:
            self._references.popitem(last=False)
        permit = CURRENT_INVOCATION.get()
        if permit is not None:
            permit.observations[self.boot_id] = ref
        return ref

    def _owned(self, permit) -> bool:
        if permit is None or self.runtime is None or self.runtime._closed:
            return False
        active = self.runtime._active.get(permit.operation_id)
        return permit.issuer is self.runtime and active is not None and active[1] is permit

    def make(self, command: ActionCommand, reference: ControlObservationRef | None = None) -> ActionEnvelope:
        permit = CURRENT_INVOCATION.get()
        if not self._owned(permit):
            raise ActionRejected("missing_bound_invocation")
        ref = reference if reference is not None else permit.observations.get(self.boot_id)
        if ref is None:
            raise ActionRejected("missing_control_observation")
        now = self._clock()
        return ActionEnvelope(
            robot_id=self.profile.robot_id, controller_boot_id=self.boot_id, control_epoch=permit.control_epoch,
            scene_epoch=permit.scene_epoch, profile_digest=self.profile.digest, runtime_boot_id=permit.runtime_boot_id,
            operation_id=permit.operation_id, attempt_id=permit.attempt_id, lease_epoch=permit.lease_epoch,
            resources=permit.resources, sequence=permit.next_sequence(), source=command.source,
            joint_names=self.profile.joint_names, joint_targets=command.joint_targets, gripper=command.gripper,
            produced_monotonic=now, valid_until_monotonic=min(permit.deadline, now + self.profile.max_command_age_s),
            observation=ref,
        )

    def validate(self, envelope: ActionEnvelope, permit) -> ActionEnvelope:
        envelope = ActionEnvelope.model_validate(envelope)
        if not self._owned(permit):
            raise ActionRejected("missing_bound_invocation")
        if self.sim.guard.limits is not self._limits:
            raise ActionRejected("control_profile_changed")
        expected = {
            "robot_id": self.profile.robot_id, "controller_boot_id": self.boot_id,
            "control_epoch": self.epoch, "scene_epoch": self.sim.state_epoch, "profile_digest": self.profile.digest,
            "runtime_boot_id": self.runtime.ledger.boot_id, "operation_id": permit.operation_id,
            "attempt_id": permit.attempt_id, "lease_epoch": permit.lease_epoch, "source": permit.source,
            "joint_names": self.profile.joint_names, "resources": permit.resources,
        }
        if any(getattr(envelope, key) != value for key, value in expected.items()):
            raise ActionRejected("action_identity_or_layout_mismatch")
        if permit.scene_epoch != self.sim.state_epoch or permit.control_epoch != self.epoch:
            raise ActionRejected("invocation_epoch_mismatch")
        if not set(self.profile.resources).issubset(permit.resources):
            raise ActionRejected("action_resources_missing")
        now = self._clock()
        if (not math.isfinite(now) or now < envelope.produced_monotonic or now >= envelope.valid_until_monotonic
                or envelope.valid_until_monotonic > permit.deadline
                or envelope.valid_until_monotonic - envelope.produced_monotonic > self.profile.max_command_age_s + 1e-9):
            raise ActionRejected("action_expired_or_invalid_deadline")
        ref = envelope.observation
        if (self._references.get(ref.reference_id) != ref or ref.control_epoch != self.epoch
                or ref.scene_epoch != self.sim.state_epoch or ref.controller_boot_id != self.boot_id
                or ref.profile_digest != self.profile.digest or ref.robot_id != self.profile.robot_id):
            raise ActionRejected("observation_reference_invalid")
        age = now - ref.captured_monotonic
        if age < 0 or age > self.profile.max_observation_age_s:
            raise ActionRejected("control_observation_stale")
        if envelope.sequence <= permit.accepted_sequence:
            raise ActionRejected("action_sequence_replayed")
        permit.accepted_sequence = envelope.sequence
        return envelope

    def can_step(self, permit) -> bool:
        if permit is None:
            return (self.runtime is not None and not self.runtime._closed and not self.runtime.observation_pending
                    and self.runtime.arbiter.idle(self.profile.resources))
        allowed = (
            self._owned(permit) and self.sim.guard.limits is self._limits
            and permit.scene_epoch == self.sim.state_epoch and permit.control_epoch == self.epoch
            and set(self.profile.resources).issubset(permit.resources)
            and permit.action_deadline is not None and self._clock() < permit.action_deadline
        )
        if not allowed:
            permit.reject("step_without_current_action")
        return allowed

    def require_idle(self) -> None:
        if self.runtime is not None and self.runtime.observation_pending:
            raise ActionRejected("observation_worker_busy")
        if self.runtime is not None and (CURRENT_INVOCATION.get() is not None
                                        or not self.runtime.arbiter.idle(self.profile.resources)):
            raise ActionRejected("controller_not_idle")


def write_action(emb, command: ActionCommand, *, observation=None):
    """Use real producer observations; do not refresh policy input after inference.

    使用生产方真实观察，不在推理后刷新策略输入时间戳。
    """
    if observation is not None and observation.object_evidence is not None:
        validator = getattr(emb, "validate_object_observation", None)
        if validator is None or not validator(observation):
            reject = getattr(emb, "reject_action", None)
            return (reject("semantic_observation_stale") if reject is not None
                    else WriteResult(applied=False, reason="semantic_observation_stale"))
    prepare = getattr(emb, "prepare_action", None)
    if prepare is None or emb.action_controller.runtime is None:
        return emb.write(command)
    permit = CURRENT_INVOCATION.get()
    if permit is not None:
        command.source = permit.source
    if observation is None:
        observation = emb.read_control_feedback()
    if observation.reference is None:
        return emb.reject_action("missing_control_observation")
    try:
        envelope = prepare(command, observation.reference)
    except (ActionRejected, ValueError) as exc:
        return emb.reject_action(str(exc))
    return emb.write_envelope(envelope)
