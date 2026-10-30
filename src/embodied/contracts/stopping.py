"""Trusted stop profiles and simulation evidence. 受信停止配置与仿真观测证据。"""

from __future__ import annotations

import hashlib
import json
import math
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StopRejected(RuntimeError):
    pass


class StopModel(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid", allow_inf_nan=False)


class StopProfile(StopModel):
    profile_id: Literal["mujoco-hold-current-v1"] = "mujoco-hold-current-v1"
    robot_id: str = Field(min_length=1)
    asset_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    resources: tuple[str, ...] = ("arm", "gripper", "workspace")
    joint_names: tuple[str, ...]
    joint_lower: tuple[float, ...]
    joint_upper: tuple[float, ...]
    max_velocity_rad_s: float = Field(default=0.02, gt=0)
    stable_duration_s: float = Field(default=0.1, gt=0)
    max_sim_duration_s: float = Field(default=2.0, gt=0)
    max_wall_duration_s: float = Field(default=2.0, gt=0)
    evidence_max_age_s: float = Field(default=1.0, gt=0)
    soft_limit_tolerance_rad: float = Field(default=0.01, ge=0)
    gripper_policy: Literal["maintain_bilateral_contact_else_hold"] = "maintain_bilateral_contact_else_hold"
    min_contact_force_n: float = Field(default=0.01, gt=0)

    @model_validator(mode="after")
    def _shape(self):
        count = len(self.joint_names)
        if not count or len(self.joint_lower) != count or len(self.joint_upper) != count:
            raise ValueError("invalid stop joint layout")
        if len(set(self.joint_names)) != count or any(a > b for a, b in zip(self.joint_lower, self.joint_upper)):
            raise ValueError("invalid stop joint limits")
        if not self.resources or not all(self.resources) or len(set(self.resources)) != len(self.resources):
            raise ValueError("invalid stop resources")
        if self.max_sim_duration_s < self.stable_duration_s:
            raise ValueError("stop horizon is shorter than the observation window")
        return self

    @property
    def digest(self) -> str:
        raw = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()


class StopRequest(StopModel):
    operation_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    attempt_boot_id: str = Field(min_length=1)
    observer_boot_id: str = Field(min_length=1)
    robot_id: str = Field(min_length=1)
    lease_epoch: int = Field(ge=1)
    resources: tuple[str, ...]
    nonce: str = Field(min_length=1)
    deadline_monotonic: float = Field(ge=0)


class StopSample(StopModel):
    sim_t: float = Field(ge=0)
    positions: tuple[float, ...]
    velocities: tuple[float, ...]
    grip_forces: tuple[float, ...] = ()


class StopEvidence(StopRequest):
    api_version: Literal["embodied.dev/v1alpha1"] = "embodied.dev/v1alpha1"
    source: Literal["simulation"] = "simulation"
    profile_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    scene_epoch: str = Field(min_length=1)
    observed_at_unix_s: float = Field(gt=0)
    measured_at_monotonic: float = Field(ge=0)
    hold_targets: tuple[float, ...]
    initial_positions: tuple[float, ...]
    previous_gripper_target: float
    gripper_strategy: Literal["hold_position", "maintain_contact"] = "hold_position"
    contact_objects: tuple[str, ...] = ()
    samples: tuple[StopSample, ...] = Field(min_length=2, max_length=2048)


def validate_stop_evidence(evidence: StopEvidence, request: StopRequest, profile: StopProfile,
                           *, now: float | None = None, historical: bool = False) -> None:
    if any(getattr(evidence, key) != value for key, value in request.model_dump().items()):
        raise StopRejected("stop_identity_mismatch")
    if evidence.robot_id != profile.robot_id or evidence.profile_digest != profile.digest:
        raise StopRejected("stop_profile_mismatch")
    if set(evidence.resources) != set(profile.resources):
        raise StopRejected("stop_resource_mismatch")
    if evidence.measured_at_monotonic > request.deadline_monotonic:
        raise StopRejected("stop_request_expired")
    if not historical:
        age = (time.monotonic() if now is None else now) - evidence.measured_at_monotonic
        if not math.isfinite(age) or age < 0 or age > profile.evidence_max_age_s:
            raise StopRejected("stop_evidence_stale")
    size = len(profile.joint_names)
    if len(evidence.hold_targets) != size or len(evidence.initial_positions) != size:
        raise StopRejected("stop_target_shape")
    if any(not lo <= value <= hi for lo, value, hi in
           zip(profile.joint_lower, evidence.hold_targets, profile.joint_upper)):
        raise StopRejected("stop_target_out_of_range")
    initial_targets = tuple(min(max(value, lo), hi) for lo, value, hi in
                            zip(profile.joint_lower, evidence.initial_positions, profile.joint_upper))
    if any(abs(a - b) > profile.soft_limit_tolerance_rad for a, b in zip(initial_targets, evidence.initial_positions)):
        raise StopRejected("stop_initial_feedback_out_of_range")
    if evidence.hold_targets[:-1] != initial_targets[:-1]:
        raise StopRejected("stop_arm_target_changed")
    if evidence.gripper_strategy == "hold_position":
        if evidence.contact_objects or evidence.hold_targets[-1] != initial_targets[-1]:
            raise StopRejected("invalid_gripper_hold")
    elif (not evidence.contact_objects or len(set(evidence.contact_objects)) != len(evidence.contact_objects)
          or evidence.hold_targets[-1] != evidence.previous_gripper_target
          or evidence.hold_targets[-1] > evidence.initial_positions[-1]):
        raise StopRejected("invalid_contact_hold")
    previous = None
    for sample in evidence.samples:
        if len(sample.positions) != size or len(sample.velocities) != size:
            raise StopRejected("stop_sample_shape")
        if previous is not None and sample.sim_t <= previous:
            raise StopRejected("stop_sample_time_not_advancing")
        if any(abs(v) > profile.max_velocity_rad_s for v in sample.velocities):
            raise StopRejected("motion_not_stopped")
        if len(sample.grip_forces) != 2 * len(evidence.contact_objects):
            raise StopRejected("stop_contact_shape")
        if any(force < profile.min_contact_force_n for force in sample.grip_forces):
            raise StopRejected("grip_contact_not_preserved")
        tolerance = profile.soft_limit_tolerance_rad
        if any(not lo - tolerance <= pos <= hi + tolerance for lo, pos, hi in
               zip(profile.joint_lower, sample.positions, profile.joint_upper)):
            raise StopRejected("stop_feedback_out_of_range")
        previous = sample.sim_t
    if evidence.samples[-1].sim_t - evidence.samples[0].sim_t + 1e-9 < profile.stable_duration_s:
        raise StopRejected("stop_window_too_short")
