"""Invocation success cannot release effects that are false or unknown."""

import pytest

from embodied.cognition.engine import PlannerEngine
from embodied.cognition.executor import DagExecutor
from embodied.cognition.offline import ScriptedPlanProvider
from embodied.cognition.plan import Plan, PlanContext, Step, StepResult, StepStatus
from embodied.skills.manifest import SkillManifest
from embodied.skills.registry import SkillRegistry, SkillResult


@pytest.mark.parametrize("mode", ["schema", "state_predicate"])
async def test_missing_effect_blocks_dependency_and_success_report(mode):
    calls = []

    async def action():
        calls.append("called")
        return SkillResult(ok=True)

    registry = SkillRegistry()
    verification = {"mode": mode, "expect": {"data_keys": ["object"], "predicate": "gripper_holding"}}
    registry.register(SkillManifest(name="skill.test.first", description="first", verification=verification), action)
    registry.register(SkillManifest(name="skill.test.next", description="next"), action)
    plan = Plan(steps=[
        Step(id="a", skill="skill.test.first", verification=verification),
        Step(id="b", skill="skill.test.next", depends_on=["a"]),
    ])
    results = [r async for r in DagExecutor(registry).run(plan, PlanContext())]
    assert calls == ["called"]
    by_id = {r.step_id: r for r in results}
    assert by_id["a"].data["_verify"]["verdict"] == ("unsat" if mode == "schema" else "unknown")
    assert by_id["b"].status == StepStatus.SKIPPED
    engine = PlannerEngine(ScriptedPlanProvider([]), registry)
    assert not engine._compose("task", results).startswith("已完成")


async def test_timeout_replay_never_becomes_success():
    calls = []

    async def action():
        calls.append("unsafe retry")
        return SkillResult(ok=True)

    registry = SkillRegistry()
    registry.register(SkillManifest(name="skill.test.action", description="action"), action)
    executor = DagExecutor(registry)
    step = Step(id="new", skill="skill.test.action")
    prior = StepResult(
        step_id="old", status=StepStatus.FAILED, error="step_timeout",
        fingerprint=executor._fingerprint(step),
    )
    results = [r async for r in executor.run(Plan(steps=[step]), PlanContext(), done={"old": prior})]
    assert not calls
    assert results[0].status == StepStatus.FAILED and results[0].error == "step_timeout"


async def test_replanning_passes_prior_results_to_executor():
    calls = []

    async def action():
        calls.append("action")
        return SkillResult(ok=True)

    class Provider(ScriptedPlanProvider):
        def __init__(self):
            super().__init__([])
            self.round = 0

        async def complete_tools(self, *args, **kwargs):
            self.round += 1
            steps = [{"id": f"s{self.round}", "skill": "skill.test.action", "params": {}}]
            return "", "test", "tool_use", (0, 0), [{
                "id": "plan", "name": "submit_plan",
                "arguments": {"complexity": "adaptive", "goal": "test", "steps": steps},
            }]

    registry = SkillRegistry()
    registry.register(SkillManifest(name="skill.test.action", description="action"), action)
    result = await PlannerEngine(Provider(), registry).turn("task")
    assert result.replans > 0
    assert calls == ["action"]
