"""Versioned reference-state and cross-host observation clock contracts (D019/D036).

版本化参考状态与跨机观察时钟契约（D019/D036），纯类型无设备或传输层依赖。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ClockDomain = Literal[
    "simulation",
    "local_monotonic",
    "rpc_rtt_bounded",
    "wall_ntp",
    "wall_ptp",
]
ClockSyncStatus = Literal[
    "not_applicable",
    "synchronized",
    "degraded",
    "unsynchronized",
]
ClockMeasurementSource = Literal[
    "simulation_clock",
    "local_os_monotonic",
    "rpc_rtt_probe",
    "ntp_declared",
    "ptp_declared",
]
ObservationSource = Literal[
    "sim_truth",
    "perceived_sim",
    "vision",
    "kinematic",
]


class FiniteModel(BaseModel):
    """Strict finite-only contract base model. 严格且仅接受有限数值的契约基类。"""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)


class ObjectPose(FiniteModel):
    """Rigid-body pose in world coordinates (meters + unit quaternion wxyz).

    世界坐标系下的刚体位姿（米与 wxyz 四元数）。
    """

    pos: tuple[float, float, float]
    quat: tuple[float, float, float, float]


class WorkspaceRegion(FiniteModel):
    """Axis-aligned workspace region with non-negative half extents.

    非负半边长的轴对齐工作空间区域。
    """

    center: tuple[float, float, float]
    half: tuple[float, float, float]

    @model_validator(mode="after")
    def _dimensions(self):
        if any(value < 0 for value in self.half):
            raise ValueError("region half extents must be nonnegative")
        return self


class ClockMetadata(FiniteModel):
    """Explicit clock-domain, host provenance, and skew/drift bounds (D036).

    显式时钟域、主机来源与偏斜/漂移上界元数据（D036）；严禁跨主机直接比较 monotonic。
    """

    clock_domain: ClockDomain = "simulation"
    clock_id: str = Field(min_length=1)
    host_id: str = Field(min_length=1)
    sync_status: ClockSyncStatus = "not_applicable"
    max_clock_skew_s: float = Field(default=0.0, ge=0.0)
    clock_drift_ppm: float = Field(default=0.0, ge=0.0, le=5000.0)
    source_unix_s: float | None = Field(default=None, gt=0.0)
    publish_unix_s: float | None = Field(default=None, gt=0.0)
    measurement_source: ClockMeasurementSource = "simulation_clock"

    @model_validator(mode="after")
    def _validate_domain_consistency(self):
        expected_source = {
            "simulation": "simulation_clock",
            "local_monotonic": "local_os_monotonic",
            "rpc_rtt_bounded": "rpc_rtt_probe",
            "wall_ntp": "ntp_declared",
            "wall_ptp": "ptp_declared",
        }[self.clock_domain]
        if self.measurement_source != expected_source:
            raise ValueError(f"measurement_source {self.measurement_source!r} mismatches clock_domain {self.clock_domain!r}")
        if self.clock_domain in ("wall_ntp", "wall_ptp"):
            if self.source_unix_s is None:
                raise ValueError("wall clock domains require source_unix_s")
            if self.sync_status == "not_applicable":
                raise ValueError("wall clock domains require explicit sync_status")
            if self.publish_unix_s is not None and self.publish_unix_s < self.source_unix_s:
                raise ValueError("publish_unix_s cannot precede source_unix_s")
        elif self.publish_unix_s is not None and self.source_unix_s is not None and self.publish_unix_s < self.source_unix_s:
            raise ValueError("publish_unix_s cannot precede source_unix_s")
        return self


class RemoteObjectEvidence(FiniteModel):
    """Cross-host per-object freshness metadata without raw remote monotonic values (D036).

    跨主机逐对象感知鲜度元数据（D036）：仅携带生产端自测年龄与 TTL，不传递供对端比较的原始单调时钟。
    """

    source: Literal["vision", "kinematic"] = "vision"
    sensor_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    scene_epoch: str = Field(min_length=1)
    calibration_id: str = Field(min_length=1)
    captured_sim_t: float = Field(ge=0.0)
    age_sim_s: float = Field(ge=0.0)
    age_wall_s: float = Field(ge=0.0)
    ttl_sim_s: float = Field(gt=0.0)
    ttl_wall_s: float = Field(gt=0.0)
    captured_unix_s: float | None = Field(default=None, gt=0.0)
    max_clock_skew_s: float = Field(default=0.0, ge=0.0)


class ClockAdmissionPolicy(FiniteModel):
    """Receiver-side cross-host clock and skew admission budget (D036).

    接收端跨主机时钟域与偏斜准入策略（D036）。
    """

    consumer_host_id: str = Field(min_length=1)
    max_age_s: float = Field(default=1.0, gt=0.0)
    max_allowed_skew_s: float = Field(default=0.1, ge=0.0)
    max_allowed_drift_ppm: float = Field(default=500.0, ge=0.0)
    max_local_wall_step_s: float = Field(default=0.5, gt=0.0)
    allow_local_monotonic_same_host: bool = True
    allow_wall_clock_domains: bool = True


class ObservationEnvelope(FiniteModel):
    """Versioned world-state envelope with optional cross-host clock & object freshness (D019/D036).

    版本化世界状态包络（D019/D036），支持跨机时钟元数据与逐对象感知鲜度契约。
    """

    api_version: Literal["embodied.dev/v1alpha1"] = "embodied.dev/v1alpha1"
    kind: Literal["WorldState"] = "WorldState"
    robot_id: str = Field(min_length=1)
    boot_id: str = Field(min_length=1)
    epoch: str = Field(min_length=1)
    sequence: int = Field(ge=1, strict=True)
    clock_domain: ClockDomain = "simulation"
    capture_timestamp: float = Field(ge=0)
    capture_duration_s: float = Field(ge=0)
    frame_id: Literal["world"] = "world"
    units: Literal["m_rad"] = "m_rad"
    calibration_version: str = Field(min_length=1)
    source: ObservationSource = "sim_truth"
    qpos: list[float]
    qvel: list[float]
    ee_pose: ObjectPose
    gripper_opening: float = Field(ge=0, le=1)
    objects: dict[str, ObjectPose]
    regions: dict[str, WorkspaceRegion]
    clock: ClockMetadata | None = None
    object_evidence: dict[str, RemoteObjectEvidence] | None = None
    object_calibration_id: str | None = None
    object_scene_epoch: str | None = None

    @model_validator(mode="after")
    def _validate_envelope(self):
        if len(self.qpos) != len(self.qvel):
            raise ValueError("joint state lengths differ")
        if self.clock is not None and self.clock.clock_domain != self.clock_domain:
            raise ValueError("clock_domain mismatch between envelope and clock metadata")
        if self.clock_domain != "simulation" and self.clock is None:
            raise ValueError("non-simulation clock_domain requires explicit clock metadata")
        if self.source == "sim_truth":
            if self.object_evidence is not None:
                raise ValueError("sim_truth envelope must not carry semantic object_evidence")
        else:
            if self.object_evidence is None:
                raise ValueError("perceived or vision envelope requires object_evidence")
            if not self.object_calibration_id or not self.object_scene_epoch:
                raise ValueError("perceived or vision envelope requires object_calibration_id and object_scene_epoch")
            missing = set(self.objects) - set(self.object_evidence)
            if missing:
                raise ValueError(f"objects missing semantic evidence: {sorted(missing)}")
            for name, ev in self.object_evidence.items():
                if ev.scene_epoch != self.object_scene_epoch:
                    raise ValueError(f"object {name!r} scene_epoch mismatch")
                if ev.calibration_id != self.object_calibration_id:
                    raise ValueError(f"object {name!r} calibration_id mismatch")
                if ev.captured_sim_t > self.capture_timestamp + 1e-9:
                    raise ValueError(f"object {name!r} captured after envelope timestamp")
        return self
