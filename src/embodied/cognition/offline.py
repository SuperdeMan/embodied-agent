"""Offline deterministic provider: degraded-mode seed and hermetic test double.

Duck-types the BaseProvider contract from embodied.providers
(complete → 4-tuple, complete_tools → 5-tuple with normalized tool_calls)
but deliberately imports nothing from that package, so the cognition loop
works — and its tests run — with zero cloud keys and zero coupling.
This is the germ of the offline degradation path promised in
docs/architecture.md §6 (断网降级); M1 replaces the keyword rules with the
ported edge NLU classifier.
"""

from __future__ import annotations

import re
from typing import Any

Call = tuple[str, dict[str, Any]]
# (pattern, tool, args) single-call form, or (pattern, [calls...]) sequence form.
Rule = tuple[str, str, dict[str, Any]] | tuple[str, list[Call]]

DEFAULT_RULES: list[Rule] = [
    (r"回零|归位|回家|\bhome\b", "skill-arm-home", {}),
    (r"挥手|打招呼|\bwave\b", "skill-arm-wave", {"times": 2}),
    (r"关机|断电|power\s*off|shutdown", "skill-system-power_off", {}),
]

# Sim tabletop rules: multi-step sequences run one call per round, driven by the
# planner feeding tool results back (pick first, then place). Order matters.
SIM_RULES: list[Rule] = [
    (r"(方块|积木|cube).*(盒|箱|bin)|收拾|整理", [("skill-manip-pick", {}), ("skill-manip-place", {})]),
    (r"抓|捡|拿起|夹起|\bpick\b|\bgrasp\b", [("skill-manip-pick", {})]),
    (r"放下|放进|放好|\bplace\b|\bdrop\b", [("skill-manip-place", {})]),
    (r"回零|归位|回家|\bhome\b", [("skill-arm-home", {})]),
]

# Plan-form rules for the PlannerEngine path (submit_plan protocol): pattern → plan dict.
PlanRule = tuple[str, dict[str, Any]]

SIM_PLAN_RULES: list[PlanRule] = [
    (
        r"(方块|积木|cube).*(盒|箱|bin)|收拾|整理",
        {
            "complexity": "simple",
            "goal": "把方块放进盒子",
            "steps": [
                {"id": "s1", "skill": "skill.manip.pick", "params": {}},
                {"id": "s2", "skill": "skill.manip.place", "params": {}, "depends_on": ["s1"]},
            ],
        },
    ),
    (r"抓|捡|拿起|夹起|\bpick\b|\bgrasp\b",
     {"steps": [{"id": "s1", "skill": "skill.manip.pick", "params": {}}]}),
    (r"放下|放进|放好|\bplace\b|\bdrop\b",
     {"steps": [{"id": "s1", "skill": "skill.manip.place", "params": {}}]}),
    (r"回零|归位|回家|\bhome\b", {"steps": [{"id": "s1", "skill": "skill.arm.home", "params": {}}]}),
]

CHAT_PLAN_RULES: list[PlanRule] = [
    (r"回零|归位|回家|\bhome\b", {"steps": [{"id": "s1", "skill": "skill.arm.home", "params": {}}]}),
    (r"挥手|打招呼|\bwave\b",
     {"steps": [{"id": "s1", "skill": "skill.arm.wave", "params": {"times": 2}}]}),
    (r"关机|断电|power\s*off|shutdown",
     {"steps": [{"id": "s1", "skill": "skill.system.power_off", "params": {}}]}),
]

_RESULTS_TAG = "<tool_results>"


def _last_user(messages: list[dict[str, Any]]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user" and not str(m.get("content", "")).startswith(_RESULTS_TAG):
            content = str(m.get("content", ""))
            # PlanBuilder user messages embed the skill catalog; match only the utterance
            # after the final 用户说: marker or rule patterns hit catalog text (e.g. "home"
            # inside a skill description).
            if "用户说:" in content:
                return content.rsplit("用户说:", 1)[-1].strip()
            return content
    return ""


def _summarize(content: str) -> str:
    inner = content.split(_RESULTS_TAG, 1)[-1].split("</tool_results>", 1)[0].strip()
    return "执行完成：\n" + inner if inner else "执行完成。"


def _normalize(rules: list[Rule]) -> list[tuple[str, list[Call]]]:
    out: list[tuple[str, list[Call]]] = []
    for rule in rules:
        if len(rule) == 3:
            pattern, tool, args = rule  # type: ignore[misc]
            out.append((pattern, [(str(tool), dict(args))]))
        else:
            pattern, seq = rule  # type: ignore[misc]
            out.append((pattern, [(str(t), dict(a)) for t, a in seq]))
    return out


class ScriptedToolProvider:
    """Maps keyword rules to tool calls (single or sequential); echoes otherwise.

    Sequences are drained one call per round: the planner executes a call, feeds the
    result back, and the next round pops the next call — same cadence a real LLM
    produces, so the planner code path is identical. Fully deterministic.
    """

    def __init__(self, rules: list[Rule] | None = None):
        self.rules = _normalize(list(DEFAULT_RULES) if rules is None else rules)
        self._calls = 0
        self._pending: list[Call] = []

    async def complete(self, messages, model, temperature, max_tokens, thinking=None, timeout_s=None):
        user = _last_user(messages)
        return f"[offline] {user}", "scripted", "stop", (0, 0)

    async def complete_tools(
        self, messages, model, temperature, max_tokens, tools=None, tool_choice=None, thinking=None, timeout_s=None
    ):
        allowed = {t.get("function", {}).get("name") for t in (tools or [])}
        last = messages[-1] if messages else {}
        content = str(last.get("content", ""))
        if last.get("role") == "user" and content.startswith(_RESULTS_TAG):
            if "ok=False" in content or "NOT EXECUTED" in content:
                self._pending.clear()  # a failed step aborts the rest of the sequence
                return _summarize(content), "scripted", "stop", (0, 0), []
            if self._pending:
                return "", "scripted", "tool_use", (0, 0), [self._emit(self._pending.pop(0))]
            return _summarize(content), "scripted", "stop", (0, 0), []
        user = _last_user(messages)
        for pattern, seq in self.rules:
            if re.search(pattern, user, re.IGNORECASE) and all(t in allowed for t, _ in seq):
                self._pending = list(seq)
                return "", "scripted", "tool_use", (0, 0), [self._emit(self._pending.pop(0))]
        text, used, finish, usage = await self.complete(messages, model, temperature, max_tokens)
        return text, used, finish, usage, []

    def _emit(self, call: Call) -> dict[str, Any]:
        self._calls += 1
        tool, args = call
        return {"id": f"scripted-{self._calls}", "name": tool, "arguments": dict(args)}


class ScriptedPlanProvider:
    """Offline provider for the PlannerEngine path: keyword rules → submit_plan calls.

    Deterministic twin of a planning LLM: build prompts get a matched plan (or empty
    steps → chat path), replan prompts get empty steps (done — scripted plans are
    single-shot). complete() answers chat turns with a recognizable offline echo.
    """

    def __init__(self, rules: list[PlanRule] | None = None):
        self.rules = list(rules or [])
        self._calls = 0

    async def complete(self, messages, model, temperature, max_tokens, thinking=None, timeout_s=None):
        user = _last_user(messages)
        return f"[offline] {user}", "scripted-plan", "stop", (0, 0)

    async def complete_tools(
        self, messages, model, temperature, max_tokens, tools=None, tool_choice=None, thinking=None, timeout_s=None
    ):
        system = str(messages[0].get("content", "")) if messages else ""
        plan: dict[str, Any] = {"steps": []}
        if "再规划器" not in system:  # replans always report done; build prompts match rules
            user = _last_user(messages)
            for pattern, p in self.rules:
                if re.search(pattern, user, re.IGNORECASE):
                    plan = p
                    break
        self._calls += 1
        call = {
            "id": f"scripted-plan-{self._calls}",
            "name": "submit_plan",
            "arguments": {k: (list(v) if isinstance(v, list) else v) for k, v in plan.items()},
        }
        return "", "scripted-plan", "tool_use", (0, 0), [call]
