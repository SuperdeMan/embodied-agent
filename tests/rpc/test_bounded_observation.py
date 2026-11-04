"""Blocked reads must not block control RPCs. 观察阻塞不得堵住控制 RPC。"""

import asyncio
import threading
import time

import pytest

grpc = pytest.importorskip("grpc")
pytest.importorskip("mujoco")

from embodied.runtime.rpcgen import GEN_DIR  # noqa: E402

if not (GEN_DIR / "embodiedrpc").is_dir():
    pytest.skip("proto stubs not generated", allow_module_level=True)

from embodied.control.drivers.mujoco_sim import TabletopSim  # noqa: E402
from embodied.control.service import ControlState, serve  # noqa: E402
from embodied.control.simassets import find_menagerie_model  # noqa: E402
from embodied.runtime.rpcgen import import_common, import_control  # noqa: E402
from embodied.skills.manifest import SkillManifest  # noqa: E402
from embodied.skills.registry import SkillRegistry, SkillResult  # noqa: E402
from embodied.skills.remote import RemoteSkillRegistry  # noqa: E402
from embodied.skills.scripted.manip import register_sim_skills  # noqa: E402

try:
    find_menagerie_model()
except FileNotFoundError:
    pytest.skip("menagerie assets not fetched", allow_module_level=True)


async def drain(registry):
    for _ in range(200):
        if not registry.observation_pending:
            return
        await asyncio.sleep(0.005)
    raise AssertionError("observer did not finish")


async def test_blocked_world_reader_keeps_query_halt_and_supervisor_responsive():
    sim = TabletopSim()
    registry = SkillRegistry(robot_id=sim.spec().embodiment_id, observation_timeout_s=0.1)
    register_sim_skills(registry, sim)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def reader():
        calls.append(1)
        entered.set()
        assert release.wait(2)
        return sim.read_reference_state()

    state = ControlState(registry, sim, world_reader=reader)
    server, port = await serve(state, port=0)
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    pb, rpc = import_control()
    stub = rpc.ControlServiceStub(channel)
    started = time.monotonic()
    reading = stub.GetWorldState(pb.WorldStateRequest())
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        query = await stub.GetOperation(pb.OperationRequest(operation_id="missing"), timeout=0.3)
        assert not query.found
        assert (await stub.Halt(pb.HaltRequest(reason="probe"), timeout=0.3)).halted

        async def pings():
            yield import_common().Timestamp(unix_ms=1)
            yield import_common().Timestamp(unix_ms=2)

        stream = stub.SupervisorLink(pings())
        assert len([pong async for pong in stream]) == 2
        assert time.monotonic() - started < 0.5
        assert not (await reading).available
        for _ in range(3):
            assert not (await stub.GetWorldState(pb.WorldStateRequest())).available
        assert calls == [1] and state.world_sequence == 0
    finally:
        release.set()
        await drain(registry)
        await channel.close()
        await server.stop(None)
        registry.close()
    assert state.world_sequence == 0


@pytest.mark.parametrize("cancel", [False, True])
async def test_effect_recheck_timeout_or_rpc_cancel_never_commits_late_evidence(cancel, monkeypatch):
    sim = TabletopSim()
    registry = SkillRegistry(robot_id=sim.spec().embodiment_id, observation_timeout_s=0.1)
    register_sim_skills(registry, sim)

    async def claim():
        return SkillResult(ok=True)

    manifest = SkillManifest(name="skill.test.proof", description="proof probe", resources=["arm", "gripper", "workspace"],
                             verification={"mode": "state_predicate", "expect": {"predicate": "object_near",
                             "args": {"object": "obj_cube", "pos": [0.0, 0.0, 0.0], "radius": 1.0}}})
    registry.register(manifest, claim)
    result = await registry.invoke(manifest.name)
    assert result.data["_effect"]["verdict"] == "sat"
    entered, release = threading.Event(), threading.Event()
    original = registry._effect_observer.capture

    def blocked(*args, **kwargs):
        if not kwargs.get("unavailable"):
            entered.set()
            assert release.wait(2)
        return original(*args, **kwargs)

    monkeypatch.setattr(registry._effect_observer, "capture", blocked)
    server, port = await serve(ControlState(registry, sim), port=0)
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    try:
        remote = await RemoteSkillRegistry(channel).connect()
        pending = asyncio.create_task(remote.reconcile_effect(result.operation_id, result.attempt_id))
        assert await asyncio.to_thread(entered.wait, 1)
        assert (await remote.get_operation(result.operation_id)).state == "RETURNED"
        if cancel:
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            for _ in range(100):
                if registry.get_operation(result.operation_id).effect_evidence["verdict"] == "unknown":
                    break
                await asyncio.sleep(0.005)
        else:
            assert (await pending).state == "RETURNED"
        proof = registry.get_operation(result.operation_id).effect_evidence
        assert proof["verdict"] == "unknown"
        assert await remote.effect_verdict(result.operation_id, result.attempt_id,
                                           result.data["_effect"]["evidence_id"], manifest.verification) == "unknown"
        release.set()
        await drain(registry)
        assert registry.get_operation(result.operation_id).effect_evidence == proof
    finally:
        release.set()
        await drain(registry)
        await channel.close()
        await server.stop(None)
        registry.close()
    assert registry._closed
    assert proof["verdict"] == "unknown"
