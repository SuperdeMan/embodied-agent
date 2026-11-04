"""Task-journal inputs; manifests remain receiver-owned. 任务日志输入；manifest 权威留在接收端。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class TaskStepInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=1, max_length=160)
    skill: str = Field(min_length=1, max_length=160)
    params: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list, max_length=128)
    param_refs: dict[str, str] = Field(default_factory=dict)
    operation_id: str = ""


class TaskPlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    api_version: Literal["embodied.dev/v1alpha1"] = "embodied.dev/v1alpha1"
    task_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.:-]+$")
    revision: int = Field(ge=1)
    goal: str = Field(default="", max_length=8192)
    steps: list[TaskStepInput] = Field(min_length=1, max_length=128)


class TaskJournalError(RuntimeError):
    pass
