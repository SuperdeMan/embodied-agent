"""Versioned ModelBundle, ReleaseBundle, and rollback preflight contracts (D037).

版本化模型包、发行包与维护态兼容/回滚预检纯契约（D037），零设备与存储驱动依赖。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


def _check_sha256(value: str, label: str) -> str:
    if not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{label} must be 64 lowercase hex chars")
    return value


class ReleaseStrictModel(BaseModel):
    """Strict finite-only base contract for release & compatibility artifacts.

    严格且仅接受有限数值的发行契约基类。
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True, frozen=True)


class ModelBundle(ReleaseStrictModel):
    """Semantic model deployment contract: matching tensor shape never implies physical compatibility.

    模型部署语义契约：输入输出维度相同不等于物理兼容，必须同时绑定本体、关节顺序、单位、动作语义与评测证据。
    """

    api_version: Literal["embodied.dev/v1alpha1"] = "embodied.dev/v1alpha1"
    kind: Literal["ModelBundle"] = "ModelBundle"
    model_id: str = Field(min_length=1)
    skill_name: str = Field(min_length=1)
    artifact_path: str = Field(min_length=1)
    artifact_sha256: str = Field(min_length=64, max_length=64)
    robot_id: str = Field(min_length=1)
    joint_order: tuple[str, ...] = Field(min_length=1)
    units: Literal["m_rad"] = "m_rad"
    action_semantics: Literal["absolute_joint_targets", "delta_joint_targets", "cartesian_delta"] = (
        "absolute_joint_targets"
    )
    normalization_mode: Literal["baked_in_graph", "external_stats"] = "baked_in_graph"
    state_dim: int = Field(ge=1)
    env_dim: int = Field(ge=0)
    action_dim: int = Field(ge=1)
    chunk_size: int = Field(ge=1)
    control_hz: float = Field(gt=0.0)
    backend: Literal["onnxruntime_cpu", "onnxruntime_cuda", "tensorrt", "torch_cpu"] = "onnxruntime_cpu"
    precision: Literal["fp32", "fp16", "int8"] = "fp32"
    license: str = Field(min_length=1)
    eval_task_id: str = Field(min_length=1)
    eval_win_rate: float = Field(ge=0.0, le=1.0)
    eval_evidence_sha256: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def _validate_model_bundle(self):
        _check_sha256(self.artifact_sha256, "artifact_sha256")
        _check_sha256(self.eval_evidence_sha256, "eval_evidence_sha256")
        if len(set(self.joint_order)) != len(self.joint_order):
            raise ValueError("joint_order contains duplicate joint names")
        if self.action_semantics in ("absolute_joint_targets", "delta_joint_targets"):
            if len(self.joint_order) != self.action_dim:
                raise ValueError("joint_order length must match action_dim for joint-target semantics")
        return self


class TargetEnvironment(ReleaseStrictModel):
    """Target robot, stop profile, driver firmware, and supported inference backends for preflight.

    预检目标运行环境：绑定本体标识、资产与停止摘要、关节序、固件版本与受支持的动作语义/后端。
    """

    api_version: Literal["embodied.dev/v1alpha1"] = "embodied.dev/v1alpha1"
    kind: Literal["TargetEnvironment"] = "TargetEnvironment"
    robot_id: str = Field(min_length=1)
    asset_digest: str = Field(min_length=64, max_length=64)
    stop_profile_digest: str = Field(min_length=64, max_length=64)
    joint_order: tuple[str, ...] = Field(min_length=1)
    units: Literal["m_rad"] = "m_rad"
    supported_backends: tuple[str, ...] = ("onnxruntime_cpu",)
    supported_action_semantics: tuple[str, ...] = ("absolute_joint_targets",)
    supported_normalization_modes: tuple[str, ...] = ("baked_in_graph",)
    driver_firmware_version: str = Field(default="sim_reference_v1", min_length=1)

    @model_validator(mode="after")
    def _validate_target(self):
        _check_sha256(self.asset_digest, "asset_digest")
        _check_sha256(self.stop_profile_digest, "stop_profile_digest")
        if len(set(self.joint_order)) != len(self.joint_order):
            raise ValueError("joint_order contains duplicate joint names")
        return self


class ReleaseBundle(ReleaseStrictModel):
    """Maintenance-mode release descriptor binding runtime, schema range, assets, models, and rollback targets.

    维护态发行包契约：绑定提交、运行时、账本 schema 区间、资产/停止/配置摘要、模型包与显式已验证回滚目标。
    """

    api_version: Literal["embodied.dev/v1alpha1"] = "embodied.dev/v1alpha1"
    kind: Literal["ReleaseBundle"] = "ReleaseBundle"
    release_id: str = Field(min_length=1)
    commit_sha: str = Field(min_length=40, max_length=40)
    runtime_version: str = Field(min_length=1)
    robot_id: str = Field(min_length=1)
    driver_firmware_compat: tuple[str, ...] = Field(min_length=1)
    min_ledger_schema: int = Field(ge=1)
    max_ledger_schema: int = Field(ge=1)
    asset_digest: str = Field(min_length=64, max_length=64)
    stop_profile_digest: str = Field(min_length=64, max_length=64)
    config_digests: dict[str, str] = Field(default_factory=dict)
    skills_catalog_sha256: str = Field(min_length=64, max_length=64)
    model_bundles: tuple[ModelBundle, ...] = ()
    eval_evidence_sha256: str = Field(min_length=64, max_length=64)
    verified_rollback_targets: tuple[str, ...] = ()
    hardware_verified: Literal[False] = False

    @model_validator(mode="after")
    def _validate_release_bundle(self):
        if not _COMMIT_RE.fullmatch(self.commit_sha):
            raise ValueError("commit_sha must be 40 lowercase hex chars")
        if self.max_ledger_schema < self.min_ledger_schema:
            raise ValueError("max_ledger_schema cannot be less than min_ledger_schema")
        _check_sha256(self.asset_digest, "asset_digest")
        _check_sha256(self.stop_profile_digest, "stop_profile_digest")
        _check_sha256(self.skills_catalog_sha256, "skills_catalog_sha256")
        _check_sha256(self.eval_evidence_sha256, "eval_evidence_sha256")
        for rel, digest in self.config_digests.items():
            if not rel or rel.startswith(("/", "\\")):
                raise ValueError(f"config_digests path must be relative: {rel!r}")
            _check_sha256(digest, f"config_digests[{rel}]")
        return self

    @property
    def bundle_digest(self) -> str:
        """Canonical SHA-256 digest of the immutable release bundle descriptor.

        不可变发行包描述文件的规范化 SHA-256 摘要。
        """
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class LedgerBackupManifest(ReleaseStrictModel):
    """Authenticated SQLite ledger backup metadata covering file digest and full table contents.

    经认证的 SQLite 账本备份清单，记录备份文件哈希与全表确定性行摘要。
    """

    api_version: Literal["embodied.dev/v1alpha1"] = "embodied.dev/v1alpha1"
    kind: Literal["LedgerBackup"] = "LedgerBackup"
    source_release: str = Field(min_length=1)
    schema_version: int = Field(ge=1)
    backup_file: str = Field(min_length=1)
    backup_sha256: str = Field(min_length=64, max_length=64)
    all_tables_sha256: str = Field(min_length=64, max_length=64)
    legacy_history_sha256: str = Field(min_length=64, max_length=64)
    row_counts: dict[str, int]
    created_unix: float = Field(gt=0.0)

    @model_validator(mode="after")
    def _validate_manifest(self):
        _check_sha256(self.backup_sha256, "backup_sha256")
        _check_sha256(self.all_tables_sha256, "all_tables_sha256")
        _check_sha256(self.legacy_history_sha256, "legacy_history_sha256")
        if any(v < 0 for v in self.row_counts.values()):
            raise ValueError("row_counts must be non-negative")
        return self


class ReleasePreflightReport(ReleaseStrictModel):
    """Structured fail-closed decision for maintenance-mode activation or rollback.

    维护态版本激活或回滚的结构化 fail-closed 预检报告。
    """

    api_version: Literal["embodied.dev/v1alpha1"] = "embodied.dev/v1alpha1"
    kind: Literal["ReleasePreflightReport"] = "ReleasePreflightReport"
    action: Literal["activate", "rollback"]
    target_release_id: str = Field(min_length=1)
    target_commit_sha: str = Field(min_length=40, max_length=40)
    current_release_id: str | None = None
    allowed: bool
    rollback_safe: bool
    ledger_schema_version: int | None = None
    ledger_all_tables_sha256: str = ""
    reasons: tuple[str, ...] = ()
    hardware_verified: Literal[False] = False
