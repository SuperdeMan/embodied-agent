"""Confirmed stop reconciliation over the real RPC boundary. 真实 RPC 边界上的确认式停止对账。"""

import asyncio
import json
import threading

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
from embodied.runtime.rpcgen import import_control  # noqa: E402
from embodied.skills.registry import SkillRegistry  # noqa: E402
from embodied.skills.remote import RemoteSkillRegistry  # noqa: E402
from embodied.skills.scripted import motions  # noqa: E402
from embodied.skills.scripted.manip import register_sim_skills  # noqa: E402

try:
    find_menagerie_model()
except FileNotFoundError:
    pytest.skip("menagerie assets not fetched", allow_module_level=True)


async def test_rpc_stop_proof_is_controller_generated_and_does_not_cancel_a_later_owner(monkeypatch):
    sim = TabletopSim()
    registry = SkillRegistry(robot_id=sim.spec().embodiment_id)
    register_sim_skills(registry, sim)
    entered, release = threading.Event(), threading.Event()
    original = motions.move_to_q

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(motions, "move_to_q", delayed)
    server, port = await serve(ControlState(registry, sim, world_reader=sim.read_reference_state), port=0)
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    identity = OperationIdentity(task_id="task", operation_id="old")
    try:
        remote = await RemoteSkillRegistry(channel).connect()
        pending = asyncio.create_task(remote.invoke("skill.arm.home", operation=identity))
        assert await asyncio.to_thread(entered.wait, 2)
        cancelled = await remote.cancel_operation("old")
        assert not (await pending).ok
        pb2, pb2_grpc = import_control()
        stub = pb2_grpc.ControlServiceStub(channel)
        denied = await stub.ReconcileStop(pb2.StopReconciliationRequest(
            operation_id="old", attempt_id=cancelled.attempt_id,
        ))
        assert denied.reconciliation_error == "confirmation_required" and not denied.stop_evidence_json
        busy = await remote.reconcile_stop("old", cancelled.attempt_id, confirmed=True)
        assert busy.reconciliation_error == "worker_still_running"
        release.set()
        for _ in range(200):
            if registry._stop_contexts["old"].drained:
                break
            await asyncio.sleep(0.01)
        observed = await remote.reconcile_stop("old", cancelled.attempt_id, confirmed=True)
        assert not observed.reconciliation_error
        assert observed.state == "OUTCOME_UNKNOWN" and observed.stop_state == "MOTION_STOPPED_OBSERVED"
        proof = json.loads(observed.stop_evidence_json)
        assert proof["source"] == "simulation" and proof["attempt_id"] == cancelled.attempt_id
        assert json.loads(observed.result_json)["ok"] is False
        assert "maintenance.reconcile_stop" not in [m.name for m in remote.catalog()]
        entered.clear()
        release.clear()
        next_call = asyncio.create_task(remote.invoke("skill.arm.home"))
        assert await asyncio.to_thread(entered.wait, 2)
        repeated = await remote.reconcile_stop("old", cancelled.attempt_id, confirmed=True)
        assert repeated.stop_evidence_json == observed.stop_evidence_json
        release.set()
        assert (await next_call).ok
    finally:
        release.set()
        await channel.close()
        await server.stop(None)
        registry.close()
