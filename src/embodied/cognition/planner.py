"""Minimal System-2 loop for M0: chat → tool calls → skill invocation → grounded reply.

Deliberately NOT the full planner port — PlanBuilder/DagExecutor/verify land in M1
(docs/roadmap.md). This file exists to prove the S2↔skills contract end to end in
text mode, with the two safety behaviors that must exist from day one:
- dangerous skills go through an explicit confirmation channel, and
- the model must never claim a physical action happened without a tool result
  (anti false-promise, inherited from car-agent's reflux discipline).

Tool results are fed back as one plain user message rather than provider-native
tool-role messages, so the loop works with every BaseProvider implementation
regardless of multi-round tool support; M1's engine port upgrades this.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from embodied.skills.registry import ConfirmationRequired, SkillNotFound, SkillRegistry, SkillResult

ConfirmCallback = Callable[[str, dict[str, Any]], Awaitable[bool]]

SYSTEM_PROMPT = (
    "You are the cognition core of a desktop robot. You act on the physical world ONLY by "
    "calling the provided skill tools; never claim a physical action happened unless a tool "
    "result confirms it. Reply briefly, in the user's language. If a dangerous skill is "
    "refused (confirmation denied), acknowledge and stop."
)


@dataclass
class SkillCallRecord:
    skill: str
    params: dict[str, Any]
    result: SkillResult | None  # None → not executed (see note)
    note: str = ""  # not_found / denied:* / error:*


@dataclass
class PlannerTurn:
    text: str
    calls: list[SkillCallRecord] = field(default_factory=list)


@dataclass
class PlannerConfig:
    model: str = ""  # "" → provider-side default model resolution
    temperature: float = 0.2
    max_tokens: int = 1024
    max_rounds: int = 4  # LLM calls per user turn (bounded loop, car-agent LoopController spirit)
    max_calls: int = 8  # skill invocations per user turn


class TextPlanner:
    def __init__(
        self,
        provider: Any,
        registry: SkillRegistry,
        config: PlannerConfig | None = None,
        confirm: ConfirmCallback | None = None,
    ) -> None:
        self.provider = provider
        self.registry = registry
        self.config = config or PlannerConfig()
        self._confirm = confirm
        self.history: list[dict[str, str]] = []  # plain user/assistant messages only

    async def turn(self, user_text: str) -> PlannerTurn:
        cfg = self.config
        calls: list[SkillCallRecord] = []
        messages: list[dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT + "\n\n" + self._skill_hint()}]
        messages += self.history
        messages.append({"role": "user", "content": user_text})
        text = ""
        for _ in range(cfg.max_rounds):
            content, _used, _finish, _usage, tool_calls = await self.provider.complete_tools(
                messages,
                cfg.model,
                cfg.temperature,
                cfg.max_tokens,
                tools=self.registry.tool_schemas(),
                tool_choice="auto",
            )
            if not tool_calls:
                text = content or ""
                break
            lines: list[str] = []
            for tc in tool_calls:
                if len(calls) >= cfg.max_calls:
                    lines.append(f"- {tc.get('name')}: NOT EXECUTED (per-turn skill budget exhausted)")
                    continue
                rec = await self._execute(tc)
                calls.append(rec)
                lines.append(self._render(rec))
            messages.append({"role": "assistant", "content": content or "(calling skills)"})
            messages.append(
                {
                    "role": "user",
                    "content": "<tool_results>\n" + "\n".join(lines) + "\n</tool_results>\n"
                    "请根据以上真实执行结果向用户简短汇报；未执行的动作不得声称已完成。",
                }
            )
        else:
            text = text or "（本轮推理达到上限，先停在这里；已执行的动作见技能记录。）"
        if not text:
            text = "（无回复）"
        self.history.append({"role": "user", "content": user_text})
        self.history.append({"role": "assistant", "content": text})
        return PlannerTurn(text=text, calls=calls)

    async def _execute(self, tc: dict[str, Any]) -> SkillCallRecord:
        tool_name = str(tc.get("name", ""))
        args = tc.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}
        try:
            skill = self.registry.resolve_tool(tool_name)
        except SkillNotFound:
            return SkillCallRecord(skill=tool_name, params=args, result=None, note="not_found")
        manifest = self.registry.get(skill)
        confirmed = False
        if manifest.require_confirm:
            if self._confirm is None:
                return SkillCallRecord(skill=skill, params=args, result=None, note="denied:no_confirm_channel")
            confirmed = await self._confirm(skill, args)
            if not confirmed:
                return SkillCallRecord(skill=skill, params=args, result=None, note="denied:user")
        try:
            result = await self.registry.invoke(skill, args, confirmed=confirmed)
        except ConfirmationRequired:
            # unreachable when the flow above is intact; kept as belt-and-braces (SG-3 gate is in the registry)
            return SkillCallRecord(skill=skill, params=args, result=None, note="denied:registry")
        except Exception as e:
            return SkillCallRecord(skill=skill, params=args, result=None, note=f"error:{type(e).__name__}:{e}")
        return SkillCallRecord(skill=skill, params=args, result=result)

    @staticmethod
    def _render(rec: SkillCallRecord) -> str:
        if rec.result is None:
            return f"- {rec.skill}: NOT EXECUTED ({rec.note})"
        r = rec.result
        return f"- {rec.skill}: ok={r.ok} detail={r.detail}"

    def _skill_hint(self) -> str:
        names = ", ".join(m.name for m in self.registry.catalog())
        return f"Available skills: {names}."
