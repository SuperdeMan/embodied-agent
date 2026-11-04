"""Effect evidence across the RPC boundary. 跨 RPC 边界的效果证据测试。"""

import json
from types import SimpleNamespace

import pytest

grpc = pytest.importorskip("grpc")
pytest.importorskip("mujoco")

from embodied.runtime.rpcgen import GEN_DIR  # noqa: E402

if not (GEN_DIR / "embodiedrpc").is_dir():
    pytest.skip("proto stubs not generated", allow_module_level=True)

from embodied.contracts.operation import OperationIdentity  # noqa: E402
from embodied.control.drivers.mujoco_sim import TabletopSim  # noqa: E402
from embodied.control.service import ControlState, serve  # noqa: E402
from embodied.control.simassets import find_menagerie_model  # noqa: E402
from embodied.skills.registry import SkillRegistry  # noqa: E402
from embodied.skills.remote import RemoteSkillRegistry  # noqa: E402
from embodied.skills.scripted.manip import register_sim_skills  # noqa: E402

try:
    find_menagerie_model()
except FileNotFoundError:
    pytest.skip("menagerie assets not fetched", allow_module_level=True)


async def test_rpc_current_effect_history_recheck_and_replay_are_distinct():
    sim = TabletopSim(seed=42)
    registry = SkillRegistry(robot_id=sim.spec().embodiment_id)
    register_sim_skills(registry, sim)
    server, port = await serve(ControlState(registry, sim), port=0)
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    try:
        remote = await RemoteSkillRegistry(channel).connect()
        identity = OperationIdentity(task_id="task", operation_id="pick")
        result = await remote.invoke("skill.manip.pick", {"object": "obj_cube"}, operation=identity)
        assert result.ok and result.data["_effect"]["verdict"] == "sat"
        evidence = result.data["_effect"]
        verification = remote.get("skill.manip.pick").verification
        assert await remote.effect_verdict("pick", result.attempt_id, evidence["evidence_id"], verification) == "sat"
        query = await remote.get_operation("pick")
        assert json.loads(query.effect_evidence_json) == evidence
        before = sim.data.time
        checked = await remote.reconcile_effect("pick", result.attempt_id)
        assert not checked.reconciliation_error and sim.data.time == before
        denied = await remote.reconcile_effect("pick", "wrong")
        assert denied.reconciliation_error == "effect_attempt_unavailable"
        placed = await remote.invoke("skill.manip.place", {"region": "bin_region"})
        assert placed.ok and placed.data["_effect"]["verdict"] == "sat"
        place_record = registry.get_operation(placed.operation_id)
        assert place_record.stop_context["effect_context"]["bindings"] == {"held_object": "obj_cube"}
        old = await remote.get_operation("pick")
        assert json.loads(old.effect_evidence_json)["verdict"] == "sat"
        assert await remote.effect_verdict("pick", result.attempt_id, evidence["evidence_id"], verification) == "unknown"
        denied = await remote.reconcile_effect("pick", result.attempt_id)
        assert denied.reconciliation_error == "effect_context_or_owner_changed"
        replay = await remote.invoke("skill.manip.pick", {"object": "obj_cube"}, operation=identity)
        assert replay.ok and replay.data["_operation_replay"]
    finally:
        await channel.close()
        await server.stop(None)
        registry.close()


@pytest.mark.parametrize("age", [2.1, -1.0, float("nan"), float("inf")])
async def test_client_rejects_invalid_or_expired_server_age(age):
    channel = grpc.aio.insecure_channel("127.0.0.1:1")
    remote = RemoteSkillRegistry(channel)

    async def check(*args, **kwargs):
        return SimpleNamespace(eligible=True, age_s=age, verdict="sat", evidence_json="{}")

    remote._stub = SimpleNamespace(CheckEffect=check)
    try:
        assert await remote.effect_verdict("op", "attempt", "receipt", {}) == "unknown"
    finally:
        await channel.close()
