# Ported from car-agent orchestrator/cloud/models.py @ f0b08f8, changes: agent/intent/slots
# vocabulary replaced by skill/params (plan leaves are skill invocations, docs/architecture.md
# §4.2); dropped fields serving origin-domain-only concerns (clarify/emotion/route-hint & catalog
# observability, edge deployment routing, NEED_SLOT suspension — skills fail fast on missing
# params and the bounded loop replans); PlanContext trimmed to what this runtime has today.
"""Planner engine data structures.

Authority chain (D009): `require_confirm`, `timeout_s` and `verification` on a Step are
ALWAYS populated from the SkillManifest by PlanBuilder — never read from LLM output.
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
    Manifest-controlled fields: require_confirm/timeout_s/verification (authority chain)."""

    id: str
    skill: str
    params: dict = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    # Param dependencies on prior results: {"param": "s1.data.object"}.
    param_refs: dict[str, str] = field(default_factory=dict)
    require_confirm: bool = False
    timeout_s: float = 30.0
    # Post-execution verification declared by the skill manifest (dict form of
    # manifest.verification; empty = don't verify). Consumed by cognition/verify.py.
    verification: dict = field(default_factory=dict)
    status: StepStatus = StepStatus.PENDING


@dataclass
class StepResult:
    step_id: str
    status: StepStatus
    detail: str = ""
    data: dict = field(default_factory=dict)  # structured result, feeds later steps' param_refs
    error: str = ""
    # Side-effect dedup fingerprint (M2 P2 discipline from origin): set on OK results and on
    # timeouts (timeout ≠ didn't happen — never blindly re-issue the same action in-turn).
    fingerprint: str = ""


@dataclass
class Plan:
    steps: list[Step]
    raw_text: str = ""  # the user utterance this plan answers
    complexity: str = "simple"  # simple | adaptive (picks the bounded-loop budget tier)
    goal: str = ""  # one-line goal, anchor for replanning
    # Observability only (never branches orchestration): which output channel produced this
    # plan — toolcall | toolcall_salvage | no_action_chat | degraded_chat.
    plan_mode: str = "toolcall"
    raw_llm: str = ""  # last raw LLM output, kept for failure forensics


@dataclass
class ReplanDecision:
    done: bool
    steps: list[Step] = field(default_factory=list)

    def to_plan(self, goal: str = "") -> Plan:
        return Plan(steps=self.steps, complexity="adaptive", goal=goal)


@dataclass
class PlanContext:
    """Per-turn orchestration context."""

    trace_id: str = ""
    session_id: str = ""
    raw_text: str = ""


class CyclicPlan(Exception):
    pass
