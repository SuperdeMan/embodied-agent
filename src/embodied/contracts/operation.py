"""Runtime-owned logical operation identity. 运行时管理的逻辑操作身份。"""

from __future__ import annotations

from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class OperationIdentity(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    api_version: Literal["embodied.dev/v1alpha1"] = "embodied.dev/v1alpha1"
    task_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.:-]+$")
    operation_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.:-]+$")
    plan_revision: int = Field(default=1, ge=1)

    @classmethod
    def fresh(cls) -> "OperationIdentity":
        return cls(task_id=uuid4().hex, operation_id=uuid4().hex)
