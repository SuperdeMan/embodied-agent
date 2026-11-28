"""Shared real-simulation recovery scenario. 共用的真实仿真恢复场景。"""

from types import SimpleNamespace

import pytest


@pytest.fixture
async def retry_scenario(tmp_path):
    pytest.importorskip("mujoco")
    from embodied.contracts.operation import OperationIdentity
    from embodied.control.isolated_sim import IsolatedTabletopSim
    from embodied.control.simassets import find_menagerie_model
    from embodied.runtime.invocation import run_blocking
    from embodied.runtime.operations import OperationLedger
    from embodied.runtime.task_schema import upgrade_ledger
    from embodied.skills.registry import SkillRegistry, SkillResult
    from embodied.skills.scripted.manip import PICK, _pick_sync, register_sim_skills

    try:
        find_menagerie_model()
    except FileNotFoundError:
        pytest.skip("menagerie assets not fetched")
    path = tmp_path / "v3.sqlite3"
    OperationLedger(path).close()
    upgrade_ledger(path, target_version=3, apply=True)
    sim = IsolatedTabletopSim(seed=0)
    ledger = OperationLedger(path)
    registry = SkillRegistry(ledger=ledger, robot_id=sim.spec().embodiment_id)
    calls = []
    register_sim_skills(registry, sim, skip=(PICK.name,))

    async def pick(object="obj_cube"):
        calls.append(object)
        if len(calls) == 1:
            # 首次调用丢回执，后续调用使用原脚本抓取。 / Lose the first receipt; subsequent calls execute the real pick.
            return SkillResult(ok=False, detail="missing receipt", data={"_execution": {"outcome": "OUTCOME_UNKNOWN"}})
        return await run_blocking(_pick_sync, sim, object)

    registry.register(PICK, pick, tracked_workers=True)
    try:
        registry.record_task_plan({"task_id": "retry-case", "revision": 1, "steps": [{"id": "pick", "skill": PICK.name}]})
        registry.record_task_intent("retry-case", 1, "pick", {})
        op_id = registry.get_task("retry-case")["steps"][0]["operation_id"]
        identity = OperationIdentity(task_id="retry-case", operation_id=op_id)
        original = await registry.invoke(PICK.name, operation=identity)
        registry.finish_task("retry-case", interrupted=True)
        assert original.invocation_state == "OUTCOME_UNKNOWN"
        async def reconcile():
            await registry.reconcile_stop(op_id, original.attempt_id, confirmed=True)
            record = await registry.reconcile_effect_async(op_id, original.attempt_id)
            assert record.effect_evidence["verdict"] == "unsat"
        yield SimpleNamespace(sim=sim, registry=registry, ledger=ledger, calls=calls, identity=identity,
                              first=original, reconcile=reconcile, path=path)
    finally:
        await registry.drain_compute()
        registry.close()
        sim.close()
