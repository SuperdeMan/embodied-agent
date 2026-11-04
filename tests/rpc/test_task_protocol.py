"""Task journaling and recovery assessment across RPC. 跨 RPC 的任务日志与恢复评估。"""

import json

import pytest

grpc = pytest.importorskip("grpc")

from embodied.runtime.rpcgen import GEN_DIR  # noqa: E402

if not (GEN_DIR / "embodiedrpc").is_dir():
    pytest.skip("proto stubs not generated", allow_module_level=True)

from embodied.cognition.engine import PlannerEngine  # noqa: E402
from embodied.cognition.offline import ScriptedPlanProvider  # noqa: E402
from embodied.contracts.task import TaskJournalError  # noqa: E402
from embodied.control.service import ControlState, serve  # noqa: E402
from embodied.runtime.rpcgen import import_control  # noqa: E402
from embodied.skills.manifest import ParamSpec, SkillManifest  # noqa: E402
from embodied.skills.registry import SkillRegistry, SkillResult  # noqa: E402
from embodied.skills.remote import RemoteSkillRegistry  # noqa: E402


async def test_remote_engine_persists_plan_intents_and_readonly_report():
    registry = SkillRegistry()
    calls = []

    async def act(x):
        calls.append(x)
        return SkillResult(ok=True, data={"x": x})

    registry.register(SkillManifest(name="skill.test.act", description="act", params={"x": ParamSpec(type="integer")},
                                   verification={"mode": "schema", "expect": {"data_keys": ["x"]}}), act)
    server, port = await serve(ControlState(registry), port=0)
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    try:
        remote = await RemoteSkillRegistry(channel).connect()
        provider = ScriptedPlanProvider([("go", {"steps": [
            {"id": "a", "skill": "skill.test.act", "params": {"x": 1}},
            {"id": "b", "skill": "skill.test.act", "params": {"x": 2}, "depends_on": ["a"]}]})])
        turn = await PlannerEngine(provider, remote).turn("go")
        assert turn.task_id and calls == [1, 2] and turn.text.startswith("已完成")
        before = registry.ledger._db.total_changes
        report = await remote.get_task(turn.task_id)
        assert report["workflow"] == "FINISHED" and len(report["steps"]) == 2
        assert all(s["call_state"] == "RETURNED" and s["current_effect"]["verdict"] == "unknown" for s in report["steps"])
        assessed = await remote.reconcile_task(turn.task_id)
        assert not assessed["automatic_resume_allowed"] and registry.ledger._db.total_changes == before
        assert assessed["recovery_review"]["coherent"] and not assessed["recovery_review"]["execution_authorized"]
        assert assessed["recovery_review"]["authenticated_subject"] is None
        assert (await remote.list_tasks())[0]["task_id"] == turn.task_id
        with pytest.raises(TaskJournalError):
            await remote.list_tasks(100)
        pb, rpc = import_control()
        denied = await rpc.ControlServiceStub(channel).RecordTaskStep(pb.TaskStepRequest(
            task_id=turn.task_id, revision=1, step_id="b", kind="execute", payload_json=json.dumps({})))
        assert denied.error and calls == [1, 2]
    finally:
        await channel.close()
        await server.stop(None)
        registry.close()
