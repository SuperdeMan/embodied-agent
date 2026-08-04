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

Rule = tuple[str, str, dict[str, Any]]

DEFAULT_RULES: list[Rule] = [
    (r"回零|归位|回家|\bhome\b", "skill-arm-home", {}),
    (r"挥手|打招呼|\bwave\b", "skill-arm-wave", {"times": 2}),
    (r"关机|断电|power\s*off|shutdown", "skill-system-power_off", {}),
]

_RESULTS_TAG = "<tool_results>"


def _last_user(messages: list[dict[str, Any]]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user" and not str(m.get("content", "")).startswith(_RESULTS_TAG):
            return str(m.get("content", ""))
    return ""


def _summarize(content: str) -> str:
    inner = content.split(_RESULTS_TAG, 1)[-1].split("</tool_results>", 1)[0].strip()
    return "执行完成：\n" + inner if inner else "执行完成。"


class ScriptedToolProvider:
    """Maps keyword rules to tool calls; echoes otherwise. Fully deterministic."""

    def __init__(self, rules: list[Rule] | None = None):
        self.rules = list(DEFAULT_RULES) if rules is None else rules
        self._calls = 0

    async def complete(self, messages, model, temperature, max_tokens, thinking=None, timeout_s=None):
        user = _last_user(messages)
        return f"[offline] {user}", "scripted", "stop", (0, 0)

    async def complete_tools(
        self, messages, model, temperature, max_tokens, tools=None, tool_choice=None, thinking=None, timeout_s=None
    ):
        last = messages[-1] if messages else {}
        content = str(last.get("content", ""))
        if last.get("role") == "user" and content.startswith(_RESULTS_TAG):
            return _summarize(content), "scripted", "stop", (0, 0), []
        user = _last_user(messages)
        allowed = {t.get("function", {}).get("name") for t in (tools or [])}
        for pattern, tool, args in self.rules:
            if re.search(pattern, user, re.IGNORECASE) and tool in allowed:
                self._calls += 1
                call = {"id": f"scripted-{self._calls}", "name": tool, "arguments": dict(args)}
                return "", "scripted", "tool_use", (0, 0), [call]
        text, used, finish, usage = await self.complete(messages, model, temperature, max_tokens)
        return text, used, finish, usage, []
