"""Control-process action contracts, not standalone authority. 控制进程动作契约，本身不授予权限。"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ActionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False,
                             revalidate_instances="always")


class ActionProfile(ActionModel):
    robot_id: str = Field(min_length=1)
    joint_names: tuple[str, ...]
    calibration_id: str = Field(min_length=1)
    limits_digest: str = Field(min_length=1)
    resources: tuple[str, ...] = ("arm", "gripper", "workspace")
    max_command_age_s: float = Field(default=1.0, gt=0)
    max_observation_age_s: float = Field(default=2.0, gt=0)

    @property
    def digest(self) -> str:
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


class ControlObservationRef(ActionModel):
    reference_id: str = Field(min_length=1)
    robot_id: str = Field(min_length=1)
    controller_boot_id: str = Field(min_length=1)
    control_epoch: str = Field(min_length=1)
    scene_epoch: str = Field(min_length=1)
    profile_digest: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    captured_monotonic: float = Field(ge=0)
    sim_t: float = Field(ge=0)


class ActionEnvelope(ActionModel):
    api_version: Literal["embodied.dev/v1alpha1"] = "embodied.dev/v1alpha1"
    robot_id: str = Field(min_length=1)
    controller_boot_id: str = Field(min_length=1)
    control_epoch: str = Field(min_length=1)
    scene_epoch: str = Field(min_length=1)
    profile_digest: str = Field(min_length=1)
    runtime_boot_id: str = Field(min_length=1)
    operation_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    lease_epoch: int = Field(ge=1)
    resources: tuple[str, ...]
    sequence: int = Field(ge=1)
    source: Literal["skill", "teleop"]
    mode: Literal["joint_position"] = "joint_position"
    units: Literal["rad"] = "rad"
    frame_id: Literal["base"] = "base"
    gripper_mode: Literal["normalized_position"] = "normalized_position"
    joint_names: tuple[str, ...]
    joint_targets: tuple[float, ...]
    gripper: float | None = None
    produced_monotonic: float = Field(ge=0)
    valid_until_monotonic: float = Field(ge=0)
    observation: ControlObservationRef

    @model_validator(mode="after")
    def _interval_and_shape(self):
        if self.valid_until_monotonic <= self.produced_monotonic:
            raise ValueError("invalid action interval")
        if len(self.joint_targets) != len(self.joint_names):
            raise ValueError("action layout mismatch")
        return self
