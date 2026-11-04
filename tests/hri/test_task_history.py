"""Console exposes task IDs and read-only history. 控制台提供任务 ID 与只读历史。"""

from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

from embodied.cognition.engine import PlannerEngine
from embodied.cognition.offline import ScriptedPlanProvider
from embodied.hri.server import AppContext, build_app
from embodied.skills.manifest import SkillManifest
from embodied.skills.registry import SkillRegistry, SkillResult


async def test_task_id_history_and_assessment_do_not_repeat_actions():
    registry = SkillRegistry()
    calls = []

    async def act():
        calls.append(1)
        return SkillResult(ok=True)

    registry.register(SkillManifest(name="skill.test.action", description="action"), act)
    provider = ScriptedPlanProvider([("go", {"steps": [{"id": "a", "skill": "skill.test.action"}]})])
    sim = SimpleNamespace(spec=lambda: SimpleNamespace(embodiment_id="mock"))
    ctx = AppContext(sim, registry, lambda confirm: PlannerEngine(provider, registry, confirm=confirm), None, None)
    client = TestClient(TestServer(build_app(ctx)))
    await client.start_server()
    try:
        ws = await client.ws_connect("/ws/session")
        await ws.receive_json()
        await ws.send_json({"type": "text", "text": "go"})
        while True:
            event = await ws.receive_json()
            if event["type"] == "reply":
                break
        task_id = event["task_id"]
        assert task_id and calls == [1]
        before = registry.ledger._db.total_changes
        history = await (await client.get("/api/tasks/" + task_id)).json()
        assessed = await (await client.get("/api/tasks/" + task_id + "?assess=1")).json()
        listed = await (await client.get("/api/tasks")).json()
        assert history["workflow"] == "FINISHED" and not assessed["automatic_resume_allowed"]
        assert listed[0]["task_id"] == task_id and registry.ledger._db.total_changes == before
        assert (await client.get("/api/tasks/missing")).status == 404
        assert (await client.get("/api/tasks?limit=1000")).status == 400
        assert calls == [1]
        await ws.close()
    finally:
        await client.close()
        registry.close()
