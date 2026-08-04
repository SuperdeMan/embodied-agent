"""SkillManifest: the single contract between the LLM planner (System 2) and the physical world.

Adapted from car-agent's declarative AgentManifest philosophy (manifest.yaml drives
capability registration; adding one never touches the orchestration core), reshaped
for physical skills per docs/architecture.md §4.3.
"""

from __future__ import annotations

import re
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

_NAME_RE = re.compile(r"^skill\.[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")


class ParamSpec(BaseModel):
    type: Literal["string", "number", "integer", "boolean", "object", "array"] = "string"
    description: str = ""
    enum: list[Any] | None = None
    required: bool = True
    default: Any = None


class TerminationSpec(BaseModel):
    timeout_s: float = 30.0
    # Predicate descriptions; informational in M0, evaluated against WorldState from M1 on.
    success: str = ""
    failure: str = ""


class RecoverySpec(BaseModel):
    max_retries: int = 0
    on_failure: Literal["abort", "retry", "home_and_abort", "escalate"] = "escalate"


class SafetySpec(BaseModel):
    max_velocity: float | None = None
    max_force: float | None = None
    workspace: str = "default"


class SkillManifest(BaseModel):
    name: str
    description: str
    params: dict[str, ParamSpec] = Field(default_factory=dict)
    preconditions: list[str] = Field(default_factory=list)
    effects: list[str] = Field(default_factory=list)
    termination: TerminationSpec = Field(default_factory=TerminationSpec)
    recovery: RecoverySpec = Field(default_factory=RecoverySpec)
    safety: SafetySpec = Field(default_factory=SafetySpec)
    require_confirm: bool = False
    impl: Literal["scripted", "policy"] = "scripted"
    # Permission scopes the skill needs (enforced by the executor from M1 on).
    scopes: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError(f"skill name must match {_NAME_RE.pattern!r}, got {v!r}")
        return v

    @property
    def tool_name(self) -> str:
        # LLM tool ids only allow [a-zA-Z0-9_-]; dots map to '-' (segments never contain '-',
        # so the mapping is reversible in SkillRegistry.resolve_tool).
        return self.name.replace(".", "-")

    def to_tool_schema(self) -> dict[str, Any]:
        """OpenAI-style tool definition, the shape BaseProvider.complete_tools consumes."""
        props: dict[str, Any] = {}
        for pname, spec in self.params.items():
            p: dict[str, Any] = {"type": spec.type}
            if spec.description:
                p["description"] = spec.description
            if spec.enum:
                p["enum"] = spec.enum
            props[pname] = p
        required = [k for k, v in self.params.items() if v.required]
        desc = self.description
        if self.require_confirm:
            desc += " (dangerous: requires explicit user confirmation before execution)"
        return {
            "type": "function",
            "function": {
                "name": self.tool_name,
                "description": desc,
                "parameters": {"type": "object", "properties": props, "required": required},
            },
        }

    @classmethod
    def from_yaml(cls, path: Any) -> "SkillManifest":
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return cls.model_validate(data)
