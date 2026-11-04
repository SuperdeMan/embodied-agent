"""Read-only recovery review, never an execution credential. 只读恢复审查，绝非执行凭据。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RecoveryOperationBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    step_id: str
    operation_id: str
    attempt_id: str | None
    request_digest: str | None
    call_state: str
    stop_state: str
    evidence_id: str | None


class RecoveryReview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    api_version: Literal["embodied.dev/v1alpha1"] = "embodied.dev/v1alpha1"
    purpose: Literal["recovery_review_only"] = "recovery_review_only"
    task_id: str
    robot_id: str
    ledger_boot_id: str
    plan_revision: int = Field(ge=0)
    controller_boot_id: str | None
    scene_epoch: str | None
    profile_digest: str | None
    state_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    coherent: bool
    execution_authorized: Literal[False] = False
    authenticated_subject: None = None
    blockers: list[str]
    operations: list[RecoveryOperationBinding]
