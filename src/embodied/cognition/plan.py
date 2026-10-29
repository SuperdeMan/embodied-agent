# 保留移植来源；模型改用技能和参数表达，删除旧领域专用路由、情绪及补槽字段，缺参数直接失败并有界重规划。
# Ported from car-agent orchestrator/cloud/models.py @ f0b08f8, changes: agent/intent/slots
# vocabulary replaced by skill/params (plan leaves are skill invocations, docs/architecture.md
# §4.2); dropped fields serving origin-domain-only concerns (clarify/emotion/route-hint & catalog
# observability, edge deployment routing, NEED_SLOT suspension — skills fail fast on missing
# params and the bounded loop replans); PlanContext trimmed to what this runtime has today.
"""Planner engine data structures.

Authority chain (D009): `require_confirm`, `timeout_s` and `verification` on a Step are
ALWAYS populated from the SkillManifest by PlanBuilder — never read from LLM output.


规划数据结构：确认、超时、验证字段由 PlanBuilder 从 manifest 填充，永不采用模型同名输出。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"
    NEED_CONFIRM = "need_confirm"


@dataclass
class Step:
    """One node of the DAG plan. LLM-controlled fields: id/skill/params/depends_on/param_refs.
    Manifest-controlled fields: require_confirm/timeout_s/verification (authority chain).

    DAG 节点中模型只提供技能、参数、依赖与引用；确认、超时和验证来自 manifest。
    """

    id: str
    skill: str
    params: dict = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    # 参数依赖前一步的结构化结果。
    # Param dependencies on prior results: {"param": "s1.data.object"}.
    param_refs: dict[str, str] = field(default_factory=dict)
    require_confirm: bool = False
    timeout_s: float = 30.0
    # manifest 声明执行后验证；空值表示未声明，由认知验证器消费。
    # Post-execution verification declared by the skill manifest (dict form of
    # manifest.verification; empty = don't verify). Consumed by cognition/verify.py.
    verification: dict = field(default_factory=dict)
    status: StepStatus = StepStatus.PENDING


@dataclass
class StepResult:
    step_id: str
    status: StepStatus
    detail: str = ""
    # 结构化结果供后续参数引用。
    data: dict = field(default_factory=dict)  # structured result, feeds later steps' param_refs
    error: str = ""
    # 成功和超时结果保留副作用指纹；超时不证明动作未发生，轮内禁止盲目重发。
    # Side-effect dedup fingerprint (M2 P2 discipline from origin): set on OK results and on
    # timeouts (timeout ≠ didn't happen — never blindly re-issue the same action in-turn).
    fingerprint: str = ""


@dataclass
class Plan:
    steps: list[Step]
    # 本计划响应的用户原始表达。
    raw_text: str = ""  # the user utterance this plan answers
    # 复杂度选择有界循环预算档位。
    complexity: str = "simple"  # simple | adaptive (picks the bounded-loop budget tier)
    # 一句话目标作为重规划锚点。
    goal: str = ""  # one-line goal, anchor for replanning
    # 仅用于观测计划输出通道，不参与编排分支。
    # Observability only (never branches orchestration): which output channel produced this
    # plan — toolcall | toolcall_salvage | no_action_chat | degraded_chat.
    plan_mode: str = "toolcall"
    # 保留模型原始输出供失败取证。
    raw_llm: str = ""  # last raw LLM output, kept for failure forensics


@dataclass
class ReplanDecision:
    done: bool
    steps: list[Step] = field(default_factory=list)

    def to_plan(self, goal: str = "") -> Plan:
        return Plan(steps=self.steps, complexity="adaptive", goal=goal)


@dataclass
class PlanContext:
    """Per-turn orchestration context.

    单轮编排上下文。
    """

    trace_id: str = ""
    session_id: str = ""
    raw_text: str = ""


class CyclicPlan(Exception):
    pass
