"""Bind review to real simulation and Guard lifecycle. 将审查绑定到真实仿真和 Guard 生命周期。"""

import asyncio

import pytest

pytest.importorskip("mujoco")

from embodied.contracts.operation import OperationIdentity  # noqa: E402
from embodied.control.drivers.mujoco_sim import TabletopSim  # noqa: E402
from embodied.control.simassets import find_menagerie_model  # noqa: E402
from embodied.skills.manifest import SkillManifest  # noqa: E402
from embodied.skills.registry import SkillRegistry, SkillResult  # noqa: E402
from embodied.skills.scripted.manip import register_sim_skills  # noqa: E402

try:
    find_menagerie_model()
except FileNotFoundError:
    pytest.skip("menagerie assets not fetched", allow_module_level=True)


@pytest.mark.parametrize("change", ["none", "reset", "guard_cycle"])
async def test_review_binds_real_operation_without_actions_or_writes(monkeypatch, change):
    sim = TabletopSim()
    registry = SkillRegistry(robot_id=sim.spec().embodiment_id)
    register_sim_skills(registry, sim)

    async def observe_only():
        return SkillResult(ok=True)

    manifest = SkillManifest(name="skill.test.probe", description="probe", resources=["arm", "gripper", "workspace"],
        verification={"mode": "state_predicate", "expect": {"predicate": "object_near", "args": {
            "object": "obj_cube", "pos": [0., 0., 0.], "radius": 1.0}}})
    registry.register(manifest, observe_only)
    registry.record_task_plan({"task_id": "task", "revision": 1, "steps": [{"id": "a", "skill": manifest.name}]})
    registry.record_task_intent("task", 1, "a", {})
    op_id = registry.get_task("task")["steps"][0]["operation_id"]
    result = await registry.invoke(manifest.name, operation=OperationIdentity(task_id="task", operation_id=op_id))
    assert result.ok and result.data["_effect"]["verdict"] == "sat"
    original = registry.check_effect_async
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(*args):
        answer = await original(*args)
        entered.set()
        await release.wait()
        return answer

    monkeypatch.setattr(registry, "check_effect_async", delayed)
    before = registry.ledger._db.total_changes
    task = asyncio.create_task(registry.reconcile_task("task"))
    await asyncio.wait_for(entered.wait(), 2)
    if change == "reset":
        sim.reset()
    elif change == "guard_cycle":
        sim.guard.estop("review test")
        sim.guard.reset()
        assert not sim.guard.halted
    physical_time = sim.data.time
    release.set()
    try:
        report = await asyncio.wait_for(task, 2)
        review = report["recovery_review"]
        assert review["coherent"] == (change == "none") and not review["execution_authorized"]
        binding = review["operations"][0]
        assert binding["operation_id"] == op_id and binding["attempt_id"] == result.attempt_id
        assert binding["request_digest"] == registry.ledger.get(op_id).request_digest
        assert binding["evidence_id"] == result.data["_effect"]["evidence_id"]
        assert report["steps"][0]["current_effect"]["eligible"] == (change == "none")
        assert registry.ledger._db.total_changes == before and sim.data.time == physical_time
    finally:
        registry.close()
        sim.close()
