"""Pure RPC mTLS peer policy contracts; zero transport or vendor dependencies.

纯类型 RPC mTLS 对端策略契约，不依赖传输库或厂商 SDK。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

RpcRole = Literal["agent", "guardian", "operator"]
RpcOperatorScope = Literal["read_only", "cancel", "reconcile_effect", "reconcile_stop", "halt", "reset_guard", "retry_step"]

# 内核硬编码的方法角色矩阵，策略不能放宽。
# Hardcoded role/method matrix in core; policy files cannot widen a role beyond this map.
CONTROL_METHOD_POLICY: dict[str, tuple[frozenset[str], str | None]] = {
    "ListSkills": (frozenset({"agent", "operator"}), None),
    "InvokeSkill": (frozenset({"agent"}), None),
    "GetOperation": (frozenset({"agent", "operator"}), None),
    "CancelOperation": (frozenset({"agent", "operator"}), "cancel"),
    "ReconcileStop": (frozenset({"operator"}), "reconcile_stop"),
    "CheckEffect": (frozenset({"agent", "operator"}), None),
    "ReconcileEffect": (frozenset({"operator"}), "reconcile_effect"),
    "RecordTaskPlan": (frozenset({"agent"}), None),
    "RecordTaskStep": (frozenset({"agent"}), None),
    "FinishTask": (frozenset({"agent"}), None),
    "GetTask": (frozenset({"agent", "operator"}), None),
    "ReconcileTask": (frozenset({"agent", "operator"}), None),
    "PrepareStepRetry": (frozenset({"operator"}), "retry_step"),
    "ConfirmStepRetry": (frozenset({"operator"}), "retry_step"),
    "ListTasks": (frozenset({"agent", "operator"}), None),
    "GetWorldState": (frozenset({"agent", "operator"}), None),
    "StreamState": (frozenset({"agent", "operator"}), None),
    "Halt": (frozenset({"guardian", "operator"}), "halt"),
    "Reset": (frozenset({"operator"}), "reset_guard"),
    "SupervisorLink": (frozenset({"guardian"}), None),
}

SAFETY_METHOD_POLICY: dict[str, tuple[frozenset[str], str | None]] = {
    "ValidateCommand": (frozenset({"agent", "operator"}), None),
    "Heartbeat": (frozenset({"agent"}), None),
    "EStop": (frozenset({"operator"}), "halt"),
}


class RpcPeerRule(BaseModel):
    """One pinned mTLS client certificate mapped to a single role and explicit scopes.

    固定单张 mTLS 客户端证书指纹到唯一角色与显式作用域。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    role: RpcRole
    cert_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    spiffe_id: str | None = None
    scopes: frozenset[RpcOperatorScope] = Field(default=frozenset(), max_length=7)

    @field_validator("spiffe_id")
    @classmethod
    def valid_spiffe(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if (not value.startswith("spiffe://") or len(value) > 256 or not value.isascii()
                or any(not 33 <= ord(ch) <= 126 for ch in value) or len(value.split("/")) < 4):
            raise ValueError("spiffe_id must be a valid ASCII spiffe:// URI")
        return value

    @model_validator(mode="after")
    def role_scope_alignment(self) -> "RpcPeerRule":
        if self.role == "operator" and not self.scopes:
            raise ValueError("operator peer must declare at least one explicit scope")
        if self.role != "operator" and self.scopes:
            raise ValueError("non-operator roles cannot declare operator scopes")
        return self


class RpcAccessPolicy(BaseModel):
    """Pinned CA and client certificate policy for ControlService / SafetyService.

    控制与监督服务的根 CA 与客户端证书固定策略。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    api_version: Literal["embodied.dev/v1alpha1"] = "embodied.dev/v1alpha1"
    ca_cert_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    peers: tuple[RpcPeerRule, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def unique_identities(self) -> "RpcAccessPolicy":
        if len({peer.cert_sha256 for peer in self.peers}) != len(self.peers):
            raise ValueError("duplicate peer cert_sha256 in RPC policy")
        if len({peer.name for peer in self.peers}) != len(self.peers):
            raise ValueError("duplicate peer name in RPC policy")
        return self


class RpcTlsPaths(BaseModel):
    """Explicit PEM file paths for mTLS server or client credentials.

    用于 mTLS 服务端或客户端凭据的显式 PEM 文件路径。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    ca_cert: str = Field(min_length=1)
    cert: str = Field(min_length=1)
    key: str = Field(min_length=1)
