# Ported from car-agent orchestrator/cloud/loop.py (tier budgets, bounded replan loop,
# observation clipping) + the engine/aggregator seam @ f0b08f8, changes: single-turn engine
# instead of a streaming service; the aggregator LLM call becomes a DETERMINISTIC composer
# (grounded by construction — it can only narrate actual StepResults, never invent state;
# an LLM-polished compose can layer on later without touching this contract); suspension
# (session store) not carried — the confirm callback resolves within the turn.
"""PlannerEngine: build plan → execute DAG → verify → bounded replan → grounded report.

This replaces the M0 reactive tool loop (cognition/planner.py) as the primary System-2
path. The M0 loop stays as the degraded fallback for providers that cannot drive the
submit_plan protocol.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from embodied.cognition.executor import ConfirmCallback, DagExecutor
from embodied.cognition.plan import Plan, PlanContext, StepResult, StepStatus
from embodied.cognition.plan_builder import PlanBuilder
from embodied.skills.registry import SkillRegistry

logger = logging.getLogger("cognition.engine")

# T2 tier budgets (origin loop.py): complexity → (max replans, wall budget ms).
_TIERS = {"simple": (2, 8000), "adaptive": (3, 20000)}
_DEFAULT_TIER = "simple"


def tier_budget(complexity: str) -> tuple[int, int]:
    iters, budget = _TIERS.get(complexity or "", _TIERS[_DEFAULT_TIER])
    if complexity == "adaptive":
        iters = _env_int("PLANNER_LOOP_MAX_ITERS_COMPLEX", iters)
        budget = _env_int("PLANNER_LOOP_BUDGET_MS_COMPLEX", budget)
    return (_env_int("PLANNER_LOOP_MAX_ITERS", iters), _env_int("PLANNER_LOOP_BUDGET_MS", budget))


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "")
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def summarize(result: StepResult) -> dict:
    """Bounded, decision-relevant observation for the replanner."""
    data = dict(result.data or {})
    if len(data) > 12:
        data = dict(list(data.items())[:12])
    return {
        "step_id": result.step_id,
        "status": result.status.value,
        "data": data,
        "detail": (result.detail or "")[:160],
        "error": (result.error or "")[:120],
    }


@dataclass
class EngineTurn:
    text: str
    plan: Plan | None = None
    results: list[StepResult] = field(default_factory=list)
    replans: int = 0


class PlannerEngine:
    def __init__(
        self,
        provider: Any,
        registry: SkillRegistry,
        *,
        confirm: ConfirmCallback | None = None,
        world_fn: Callable[[], Any] | None = None,
        context_fn: Callable[[], str] | None = None,
        model: str = "",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.builder = PlanBuilder(provider, registry, model=model)
        self.executor = DagExecutor(registry, confirm=confirm, world_fn=world_fn)
        self._provider = provider
        self._context_fn = context_fn
        self._model = model
        self.history: list[dict[str, str]] = []

    async def turn(self, user_text: str) -> EngineTurn:
        ctx = PlanContext(raw_text=user_text)
        context_block = self._render_context()
        plan = await self.builder.build(user_text, context_block)

        if not plan.steps:
            text = await self._chat_reply(user_text)
            self._remember(user_text, text)
            return EngineTurn(text=text, plan=plan)

        results: list[StepResult] = []
        observations: list[dict] = []
        max_iters, budget_ms = tier_budget(plan.complexity)
        deadline = time.monotonic() + budget_ms / 1000.0
        replans = 0
        current: Plan | None = plan

        while current is not None:
            async for sr in self.executor.run(current, ctx):
                results.append(sr)
                observations.append(summarize(sr))
                observations = observations[-6:]
            current = None
            if any(r.status == StepStatus.NEED_CONFIRM for r in results):
                break  # denied confirmation ends the turn honestly; no replan second-guessing
            if plan.complexity != "adaptive":
                break
            if replans >= max_iters or time.monotonic() >= deadline:
                break
            nxt = await self.builder.replan(plan.goal or user_text, observations, self._render_context())
            replans += 1
            if nxt.steps:
                # Seed with completed results so param_refs across rounds still resolve
                # and dedup fingerprints survive the replan (anti re-execution).
                done = {r.step_id: r for r in results}
                current = nxt
                current.steps = [s for s in nxt.steps if s.id not in done]
                if not current.steps:
                    current = None

        text = self._compose(user_text, results)
        self._remember(user_text, text)
        return EngineTurn(text=text, plan=plan, results=results, replans=replans)

    # -- compose (deterministic, grounded by construction) ---------------------

    _STATUS_ZH = {
        StepStatus.OK: "完成",
        StepStatus.FAILED: "失败",
        StepStatus.SKIPPED: "跳过",
        StepStatus.NEED_CONFIRM: "未执行（需确认）",
    }

    def _compose(self, user_text: str, results: list[StepResult]) -> str:
        if not results:
            return "（没有可执行的步骤。）"
        lines = []
        all_ok = True
        for r in results:
            label = self._STATUS_ZH.get(r.status, r.status.value)
            note = r.detail or r.error or ""
            caveat = ""
            v = (r.data or {}).get("_verify") or {}
            if v.get("verdict") == "unsat":
                caveat = "（对账未通过：世界状态未见预期变化）"
                all_ok = False
            if v.get("exec") == "uncertain_confirmed":
                caveat = "（超时未收到结果，但世界状态显示可能已生效，未重复执行）"
            if r.status != StepStatus.OK:
                all_ok = False
            lines.append(f"- {r.step_id}: {label}{'：' + note if note else ''}{caveat}")
        head = "已完成。" if all_ok else "部分步骤未完成。"
        return head + "\n" + "\n".join(lines)

    async def _chat_reply(self, user_text: str) -> str:
        messages = [
            {
                "role": "system",
                "content": "你是一台桌面机器人的语音助手。用户这句话不需要任何物理动作，"
                           "用一两句话直接回答；不得声称做了任何动作。",
            },
            *self.history,
            {"role": "user", "content": user_text},
        ]
        try:
            content, *_ = await self._provider.complete(messages, self._model, 0.3, 512)
            return content or "（无回复）"
        except Exception as e:
            logger.warning("chat reply failed: %s", e)
            return "（暂时无法回复。）"

    def _render_context(self) -> str:
        parts = []
        if self._context_fn is not None:
            try:
                world = self._context_fn()
                if world:
                    parts.append(f"当前世界状态:\n{world}")
            except Exception as e:
                logger.warning("context_fn errored (ignored): %s", e)
        if self.history:
            recent = self.history[-6:]
            hist = "\n".join(f"{m['role']}: {m['content'][:120]}" for m in recent)
            parts.append(f"最近对话:\n{hist}")
        return "\n\n".join(parts)

    def _remember(self, user_text: str, reply: str) -> None:
        self.history.append({"role": "user", "content": user_text})
        self.history.append({"role": "assistant", "content": reply})
        self.history = self.history[-20:]
