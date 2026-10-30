"""Versioned reference-state envelope. 版本化参考状态包络。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FiniteModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)


class ObjectPose(FiniteModel):
    pos: tuple[float, float, float]
    quat: tuple[float, float, float, float]


class WorkspaceRegion(FiniteModel):
    center: tuple[float, float, float]
    half: tuple[float, float, float]

    @model_validator(mode="after")
    def _dimensions(self):
        if any(value < 0 for value in self.half):
            raise ValueError("region half extents must be nonnegative")
        return self


class ObservationEnvelope(FiniteModel):
    api_version: Literal["embodied.dev/v1alpha1"] = "embodied.dev/v1alpha1"
    kind: Literal["WorldState"] = "WorldState"
    robot_id: str = Field(min_length=1)
    boot_id: str = Field(min_length=1)
    epoch: str = Field(min_length=1)
    sequence: int = Field(ge=1, strict=True)
    clock_domain: Literal["simulation"] = "simulation"
    capture_timestamp: float = Field(ge=0)
    capture_duration_s: float = Field(ge=0)
    frame_id: Literal["world"] = "world"
    units: Literal["m_rad"] = "m_rad"
    calibration_version: str = Field(min_length=1)
    source: Literal["sim_truth"] = "sim_truth"
    qpos: list[float]
    qvel: list[float]
    ee_pose: ObjectPose
    gripper_opening: float = Field(ge=0, le=1)
    objects: dict[str, ObjectPose]
    regions: dict[str, WorkspaceRegion]

    @model_validator(mode="after")
    def _joints(self):
        if len(self.qpos) != len(self.qvel):
            raise ValueError("joint state lengths differ")
        return self
