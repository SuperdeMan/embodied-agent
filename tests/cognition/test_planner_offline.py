"""End-to-end M0 DoD test: text conversation drives mock skills through the planner,
offline, deterministically — no keys, no network."""

from __future__ import annotations

from typing import Any

from embodied.cognition.offline import ScriptedToolProvider
from embodied.cognition.planner import PlannerConfig, TextPlanner
from embodied.skills.builtin.mock import register_builtin
from embodied.skills.manifest import SkillManifest
from embodied.skills.registry import SkillRegistry, SkillResult


def _setup(confirm=None, rules=None) -> tuple[TextPlanner, SkillRegistry]:
    registry = SkillRegistry()
    register_builtin(registry)
    planner = TextPlanner(ScriptedToolProvider(rules), registry, PlannerConfig(), confirm=confirm)
    return planner, registry


async def test_home_command_executes_skill():
    planner, _ = _setup()
    turn = await planner.turn("让机械臂回零")
    assert [c.skill for c in turn.calls] == ["skill.arm.home"]
    assert turn.calls[0].result is not None and turn.calls[0].result.ok
    assert "arm homed" in turn.text  # reply is grounded in the actual tool result


async def test_plain_chat_no_skills():
    planner, _ = _setup()
    turn = await planner.turn("你好")
    assert turn.calls == []
    assert turn.text.startswith("[offline]")


async def test_params_flow_through():
    planner, _ = _setup()
    turn = await planner.turn("挥手打个招呼")
    assert turn.calls[0].skill == "skill.arm.wave"
    assert turn.calls[0].result.data == {"times": 2}


async def test_dangerous_skill_denied_by_default():
    """No confirmation channel → dangerous skill must NOT run."""
    planner, _ = _setup(confirm=None)
    turn = await planner.turn("把机器人关机")
    assert turn.calls[0].skill == "skill.system.power_off"
    assert turn.calls[0].result is None
    assert turn.calls[0].note == "denied:no_confirm_channel"


async def test_dangerous_skill_user_denies():
    asked: list[str] = []

    async def deny(skill: str, params: dict[str, Any]) -> bool:
        asked.append(skill)
        return False

    planner, _ = _setup(confirm=deny)
    turn = await planner.turn("power off now")
    assert asked == ["skill.system.power_off"]
    assert turn.calls[0].result is None and turn.calls[0].note == "denied:user"


async def test_dangerous_skill_user_approves():
    async def approve(skill: str, params: dict[str, Any]) -> bool:
        return True

    planner, _ = _setup(confirm=approve)
    turn = await planner.turn("shutdown")
    assert turn.calls[0].result is not None and turn.calls[0].result.ok


async def test_multi_turn_history_grows():
    planner, _ = _setup()
    await planner.turn("你好")
    await planner.turn("home")
    assert len(planner.history) == 4
    assert planner.history[0]["role"] == "user"


async def test_runaway_tool_loop_is_bounded():
    """LoopController spirit: a provider that never stops calling tools cannot spin forever."""

    class AlwaysCall:
        def __init__(self):
            self.n = 0

        async def complete_tools(self, messages, model, temperature, max_tokens, **kw):
            self.n += 1
            return "", "always", "tool_use", (0, 0), [{"id": str(self.n), "name": "skill-arm-home", "arguments": {}}]

    registry = SkillRegistry()
    register_builtin(registry)
    provider = AlwaysCall()
    cfg = PlannerConfig(max_rounds=3, max_calls=2)
    planner = TextPlanner(provider, registry, cfg)
    turn = await planner.turn("loop")
    assert provider.n == 3  # LLM loop bounded
    assert len(turn.calls) == 2  # skill budget bounded
    assert turn.text  # fallback text, not a hang or crash


async def test_runtime_registered_skill_reachable_from_llm():
    """加技能不动内核, end to end: new manifest + rule → planner runs it untouched."""
    registry = SkillRegistry()
    register_builtin(registry)
    hits: list[dict] = []

    async def blink(**kw) -> SkillResult:
        hits.append(kw)
        return SkillResult(ok=True, detail="LED blinked")

    registry.register(
        SkillManifest.model_validate({"name": "skill.led.blink", "description": "blink status LED"}),
        blink,
    )
    rules = [(r"眨|blink", "skill-led-blink", {})]
    planner = TextPlanner(ScriptedToolProvider(rules), registry, PlannerConfig())
    turn = await planner.turn("blink the light")
    assert hits == [{}]
    assert "LED blinked" in turn.text
