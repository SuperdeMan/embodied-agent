"""Durable effect observations, independent of call and stop outcomes. 独立于调用及停止结果的持久效果观察。"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

EFFECT_MAX_AGE_S = 2.0


def effect_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                                     separators=(",", ":")).encode()).hexdigest()


class EffectRejected(RuntimeError):
    pass


class EffectEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False,
                             revalidate_instances="always")
    api_version: Literal["embodied.dev/v1alpha1"] = "embodied.dev/v1alpha1"
    evidence_id: str = Field(min_length=1)
    operation_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    robot_id: str = Field(min_length=1)
    request_digest: str = Field(min_length=1)
    verification_digest: str = Field(min_length=1)
    binding_digest: str = Field(min_length=1)
    observer_boot_id: str = Field(min_length=1)
    controller_boot_id: str = Field(min_length=1)
    scene_epoch: str = Field(min_length=1)
    profile_digest: str = Field(min_length=1)
    captured_monotonic: float = Field(ge=0)
    captured_unix: float = Field(ge=0)
    verdict: Literal["sat", "unsat", "unknown"]
    reason: str = ""
    source: Literal["sim_truth", "perception", "unavailable"]
    snapshot: dict[str, Any] = Field(default_factory=dict)
    digest: str = Field(min_length=1)

    @classmethod
    def seal(cls, **values):
        values.setdefault("api_version", "embodied.dev/v1alpha1")
        values["digest"] = effect_digest(values)
        return cls.model_validate(values)

    def intact(self) -> bool:
        return self.digest == effect_digest(self.model_dump(mode="json", exclude={"digest"}))
