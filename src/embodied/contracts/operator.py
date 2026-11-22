"""Operator requests cannot supply identity or broaden scope. 操作者请求不能自报身份或扩大范围。"""

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, IPvAnyAddress, field_validator

OperatorScope = Literal["reconcile_effect", "reconcile_stop"]


class OperatorPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    api_version: Literal["embodied.dev/v1alpha1"] = "embodied.dev/v1alpha1"
    trusted_proxy: IPvAnyAddress
    origin: str
    subjects: frozenset[str] = Field(min_length=1, max_length=32)
    # 未显式声明时仅授权效果重查，避免旧策略升级后静默扩大权限。
    # Default to effect review only when omitted so existing policies never silently widen scope.
    scopes: frozenset[OperatorScope] = Field(default=frozenset({"reconcile_effect"}), min_length=1, max_length=2)

    @field_validator("trusted_proxy")
    @classmethod
    def private_proxy(cls, value):
        if not (value.is_private or value.is_loopback) or value.is_unspecified or value.is_multicast:
            raise ValueError("proxy must be one private or loopback address")
        return value

    @field_validator("origin")
    @classmethod
    def https_origin(cls, value):
        parsed = urlsplit(value)
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError("invalid origin port")
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.path or parsed.query or parsed.fragment or value != f"https://{parsed.netloc}"):
            raise ValueError("exact HTTPS origin required")
        return value

    @field_validator("subjects")
    @classmethod
    def exact_subjects(cls, values):
        if any(not s or len(s) > 320 or not s.isascii() or any(not 33 <= ord(c) <= 126 for c in s)
               or "," in s or "=?" in s for s in values):
            raise ValueError("subjects must be exact ASCII login names")
        return values


class OperatorAuthorizationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    scope: OperatorScope
    task_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.:-]+$")
    operation_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.:-]+$")
    attempt_id: str = Field(min_length=1, max_length=160)
    state_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class EffectAuthorizationRequest(OperatorAuthorizationRequest):
    scope: Literal["reconcile_effect"]


class OperatorConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    challenge_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    confirmed: bool
