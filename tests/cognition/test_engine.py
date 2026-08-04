"""PlannerEngine end-to-end tests over the offline plan provider (hermetic)."""

from __future__ import annotations

from embodied.cognition.engine import PlannerEngine, tier_budget
from embodied.cognition.offline import CHAT_PLAN_RULES, ScriptedPlanProvider
from embodied.cognition.plan import StepStatus
from embodied.skills.builtin.mock import register_builtin
from embodied.skills.registry import SkillRegistry


def make_engine(confirm=None, rules=None) -> PlannerEngine:
    registry = SkillRegistry()
    register_builtin(registry)
    return PlannerEngine(ScriptedPlanProvider(rules or CHAT_PLAN_RULES), registry, confirm=confirm)


async def test_plan_execute_report():
    engine = make_engine()
    turn = await engine.turn("让机械臂回零")
    assert turn.plan is not None and turn.plan.steps[0].skill == "skill.arm.home"
    assert [r.status for r in turn.results] == [StepStatus.OK]
    assert "完成" in turn.text and "s1" in turn.text


async def test_chat_path_no_steps():
    engine = make_engine()
    turn = await engine.turn("你好呀")
    assert turn.results == []
    assert turn.text.startswith("[offline]")  # chat reply, grounded: no fake actions


async def test_dangerous_step_fail_closed_headless():
    engine = make_engine(confirm=None)
    turn = await engine.turn("把机器人关机")
    assert turn.results[0].status == StepStatus.NEED_CONFIRM
    assert "未执行" in turn.text


async def test_dangerous_step_approved():
    async def approve(skill, params):
        return True

    engine = make_engine(confirm=approve)
    turn = await engine.turn("shutdown please")
    assert turn.results[0].status == StepStatus.OK


async def test_params_flow_from_plan_rules():
    engine = make_engine()
    turn = await engine.turn("挥手打个招呼")
    assert turn.results[0].status == StepStatus.OK
    assert turn.results[0].data == {"times": 2}


async def test_history_survives_turns():
    engine = make_engine()
    await engine.turn("你好")
    await engine.turn("回零")
    assert len(engine.history) == 4


def test_tier_budget_env_overrides(monkeypatch):
    assert tier_budget("simple") == (2, 8000)
    assert tier_budget("adaptive") == (3, 20000)
    monkeypatch.setenv("PLANNER_LOOP_MAX_ITERS", "7")
    assert tier_budget("adaptive")[0] == 7


async def test_replan_bounded_for_adaptive():
    """An adaptive plan whose replans keep emitting steps must stop at the tier budget."""

    class LoopingProvider(ScriptedPlanProvider):
        def __init__(self):
            super().__init__([])
            self.plan_calls = 0

        async def complete_tools(self, messages, model, temperature, max_tokens, **kw):
            self.plan_calls += 1
            args = {
                "complexity": "adaptive", "goal": "loop forever",
                "steps": [{"id": f"s{self.plan_calls}", "skill": "skill.arm.home", "params": {}}],
            }
            return "", "loop", "tool_use", (0, 0), [
                {"id": str(self.plan_calls), "name": "submit_plan", "arguments": args}
            ]

    registry = SkillRegistry()
    register_builtin(registry)
    provider = LoopingProvider()
    engine = PlannerEngine(provider, registry)
    turn = await engine.turn("keep going")
    max_iters, _ = tier_budget("adaptive")
    assert turn.replans <= max_iters
    assert provider.plan_calls <= max_iters + 1  # initial build + bounded replans
    assert turn.results  # something actually ran, then the loop stopped
