"""Task history cannot cross scene changes or unresolved calls. 任务历史不能跨场景或未知调用自动续跑。"""

import pytest

pytest.importorskip("mujoco")

from embodied.contracts.operation import OperationIdentity  # noqa: E402
from embodied.contracts.task import TaskJournalError  # noqa: E402
from embodied.control.drivers.mujoco_sim import TabletopSim  # noqa: E402
from embodied.control.simassets import find_menagerie_model  # noqa: E402
from embodied.skills.manifest import SkillManifest  # noqa: E402
from embodied.skills.registry import SkillRegistry, SkillResult  # noqa: E402
from embodied.skills.scripted.manip import register_sim_skills  # noqa: E402

try:
    find_menagerie_model()
except FileNotFoundError:
    pytest.skip("menagerie assets not fetched", allow_module_level=True)


def setup():
    sim = TabletopSim()
    registry = SkillRegistry(robot_id=sim.spec().embodiment_id)
    register_sim_skills(registry, sim)
    return sim, registry


def op(registry, step):
    spec = next(s for p in registry.get_task("task")["plans"] for s in p["steps"] if s["id"] == step)
    return OperationIdentity(task_id="task", operation_id=spec["operation_id"])


async def test_scene_reset_keeps_history_but_rejects_remaining_task_steps():
    sim, registry = setup()
    try:
        registry.record_task_plan({"task_id": "task", "revision": 1, "steps": [
            {"id": "a", "skill": "skill.arm.home"}, {"id": "b", "skill": "skill.arm.home", "depends_on": ["a"]}]})
        registry.record_task_intent("task", 1, "a", {})
        first = await registry.invoke("skill.arm.home", operation=op(registry, "a"))
        assert first.ok
        sim.reset()
        t = sim.data.time
        with pytest.raises(TaskJournalError, match="world_context_changed"):
            registry.record_task_intent("task", 1, "b", {})
        report = await registry.reconcile_task("task")
        assert not report["context_matches"] and not report["automatic_resume_allowed"]
        assert report["steps"][0]["call_state"] == "RETURNED"
        assert (await registry.invoke("skill.arm.home", operation=op(registry, "a"))).ok
        assert sim.data.time == t
    finally:
        registry.close()
        sim.close()


async def test_stopped_unknown_call_does_not_unlock_task_continuation():
    sim, registry = setup()

    async def uncertain():
        return SkillResult(ok=False, data={"_execution": {"outcome": "OUTCOME_UNKNOWN"}})

    registry.register(SkillManifest(name="skill.test.uncertain", description="uncertain",
                                   resources=["arm", "gripper", "workspace"]), uncertain, tracked_workers=True)
    try:
        registry.record_task_plan({"task_id": "task", "revision": 1, "steps": [
            {"id": "a", "skill": "skill.test.uncertain"},
            {"id": "b", "skill": "skill.arm.home", "depends_on": ["a"]}]})
        registry.record_task_intent("task", 1, "a", {})
        result = await registry.invoke("skill.test.uncertain", operation=op(registry, "a"))
        before = registry.ledger._db.total_changes
        report = await registry.reconcile_task("task")
        assert "stop_unconfirmed" in report["issues"] and registry.ledger._db.total_changes == before
        await registry.reconcile_stop(result.operation_id, result.attempt_id, confirmed=True)
        registry.record_task_intent("task", 1, "b", {})
        t = sim.data.time
        with pytest.raises(TaskJournalError, match="uncertain_operation"):
            await registry.invoke("skill.arm.home", operation=op(registry, "b"))
        assert sim.data.time == t
        assert not (await registry.reconcile_task("task"))["automatic_resume_allowed"]
    finally:
        registry.close()
        sim.close()
