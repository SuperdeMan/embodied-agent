"""SkillManifest: the single contract between the LLM planner (System 2) and the physical world.

Adapted from car-agent's declarative AgentManifest philosophy (manifest.yaml drives
capability registration; adding one never touches the orchestration core), reshaped
for physical skills per docs/architecture.md §4.3.


技能 manifest 是模型与物理执行的声明式契约；新增能力只注册，不改编排内核。
"""

from __future__ import annotations

import math
import re
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

_NAME_RE = re.compile(r"^skill\.[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")


class ParamSpec(BaseModel):
    type: Literal["string", "number", "integer", "boolean", "object", "array"] = "string"
    description: str = ""
    enum: list[Any] | None = None
    required: bool = True
    default: Any = None
    minimum: float | None = Field(default=None, allow_inf_nan=False)
    maximum: float | None = Field(default=None, allow_inf_nan=False)

    @model_validator(mode="after")
    def _check_bounds(self) -> "ParamSpec":
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("minimum must not exceed maximum")
        if (self.minimum is not None or self.maximum is not None) and self.type not in ("number", "integer"):
            raise ValueError("numeric bounds require a numeric parameter")
        if self.default is not None:
            self.validate_value(self.default)
        return self

    def validate_value(self, value: Any) -> None:
        """Validate JSON values without coercion; bool is not a numeric command.

        不做隐式转换地校验 JSON 值，布尔值不能充当数值命令。
        """
        types = {
            "string": (str,), "number": (int, float), "integer": (int,),
            "boolean": (bool,), "object": (dict,), "array": (list,),
        }
        if type(value) not in types[self.type]:
            raise ValueError(f"expected {self.type}")
        _validate_json(value)
        if self.enum is not None and not any(type(value) is type(item) and value == item for item in self.enum):
            raise ValueError("value is outside enum")
        if self.minimum is not None and value < self.minimum:
            raise ValueError("value is below minimum")
        if self.maximum is not None and value > self.maximum:
            raise ValueError("value exceeds maximum")


def _validate_json(value: Any) -> None:
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("non-finite number")
        return
    if type(value) is list:
        for item in value:
            _validate_json(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _validate_json(item)
        return
    raise ValueError("expected JSON-compatible value")


class TerminationSpec(BaseModel):
    timeout_s: float = Field(default=30.0, gt=0, allow_inf_nan=False)
    # 谓词描述在早期仅作说明，后续基于世界状态求值。
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
    resources: list[str] = Field(default_factory=list)

    @field_validator("resources")
    @classmethod
    def _check_resources(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values) or any(not re.fullmatch(r"[a-z][a-z0-9_.:-]*", v) for v in values):
            raise ValueError("resources must be distinct nonempty resource identifiers")
        return values
    # 技能所需权限范围声明。
    # Permission scopes the skill needs (enforced by the executor from M1 on).
    scopes: list[str] = Field(default_factory=list)
    # 执行后验证由 manifest 声明模式、期望、期限和恢复；参数支持实际值引用。引擎不分支匹配技能名，新增技能在此声明验证。
    # Post-execution reconciliation declaration, consumed by cognition/verify.py:
    # {"mode": "state_predicate"|"schema", "expect": {"predicate": ..., "args": {...}} |
    #  {"data_keys": [...]}, "timeout_ms": int, "on_fail": "report"|"retry",
    #  "max_attempts": int}. Empty = don't verify. Args support "$param:<name>" refs to
    # the step's actual params. Declarative by iron rule: the engine has no skill-name
    # branches — a new skill gets verification by declaring it HERE (car-agent M2 lesson).
    verification: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError(f"skill name must match {_NAME_RE.pattern!r}, got {v!r}")
        return v

    @property
    def tool_name(self) -> str:
        # 工具名不允许点号，映射为连字符；命名片段不含连字符，因此可逆。
        # LLM tool ids only allow [a-zA-Z0-9_-]; dots map to '-' (segments never contain '-',
        # so the mapping is reversible in SkillRegistry.resolve_tool).
        return self.name.replace(".", "-")

    def to_tool_schema(self) -> dict[str, Any]:
        """OpenAI-style tool definition, the shape BaseProvider.complete_tools consumes.

        生成模型提供方消费的 OpenAI 风格工具定义。
        """
        props: dict[str, Any] = {}
        for pname, spec in self.params.items():
            p: dict[str, Any] = {"type": spec.type}
            if spec.description:
                p["description"] = spec.description
            if spec.enum is not None:
                p["enum"] = spec.enum
            if spec.minimum is not None:
                p["minimum"] = spec.minimum
            if spec.maximum is not None:
                p["maximum"] = spec.maximum
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
                "parameters": {
                    "type": "object", "properties": props, "required": required, "additionalProperties": False,
                },
            },
        }

    @classmethod
    def from_yaml(cls, path: Any) -> "SkillManifest":
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return cls.model_validate(data)
