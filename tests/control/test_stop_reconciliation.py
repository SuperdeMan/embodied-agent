"""Observed stopping against real MuJoCo state. 基于真实 MuJoCo 状态的停止观测回归。"""

import asyncio
import json
import sqlite3
import threading
import time

import pytest

pytest.importorskip("mujoco")

from embodied.cognition.world_state import WorldSnapshot, held_object  # noqa: E402
from embodied.contracts.operation import OperationIdentity  # noqa: E402
from embodied.contracts.stopping import StopRejected, StopRequest  # noqa: E402
from embodied.control.drivers.mujoco_sim import TabletopSim  # noqa: E402
from embodied.control.hal import ActionCommand  # noqa: E402
from embodied.control.simassets import find_menagerie_model  # noqa: E402
from embodied.runtime.invocation import run_blocking  # noqa: E402
from embodied.runtime.operations import OperationLedger  # noqa: E402
from embodied.skills.manifest import SkillManifest, TerminationSpec  # noqa: E402
from embodied.skills.registry import ConfirmationRequired, SkillError, SkillRegistry, SkillResult  # noqa: E402
from embodied.skills.scripted.manip import register_sim_skills  # noqa: E402

try:
    find_menagerie_model()
except FileNotFoundError:
    pytest.skip("menagerie assets not fetched", allow_module_level=True)


def registry_for(sim, path=":memory:"):
    registry = SkillRegistry(ledger=OperationLedger(path), robot_id=sim.spec().embodiment_id)
    registry.configure_stop_controller(sim.stop_controller)
    return registry


async def drain(registry, operation_id):
    for _ in range(200):
        if registry._stop_contexts[operation_id].drained:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("worker failed to finish")


async def interrupted_motion(registry, sim, *, tracking=True):
    entered, release = threading.Event(), threading.Event()
    writes = []

    def worker():
        q = list(sim.read().qpos)
        q[0] += 0.1
        command = ActionCommand(tuple(q))
        writes.append(sim.write(command).applied)
        sim.step(20)
        entered.set()
        if not release.wait(5):
            raise RuntimeError("test worker release missing")
        writes.append(sim.write(command).applied)
        return SkillResult(ok=True)

    async def action():
        return await (run_blocking(worker) if tracking else asyncio.to_thread(worker))

    registry.register(SkillManifest(
        name="skill.test.motion", description="motion", resources=["arm", "gripper", "workspace"],
        termination=TerminationSpec(timeout_s=0.1),
    ), action, tracked_workers=tracking)
    identity = OperationIdentity(task_id="task", operation_id="interrupted")
    task = asyncio.create_task(registry.invoke("skill.test.motion", operation=identity))
    assert await asyncio.to_thread(entered.wait, 2)
    result = await task
    assert result.invocation_state == "OUTCOME_UNKNOWN" and not result.ok
    return identity, result, release, writes


async def test_late_worker_must_drain_then_observed_stop_releases_only_new_operations():
    sim = TabletopSim()
    registry = registry_for(sim)
    identity, result, release, writes = await interrupted_motion(registry, sim)
    try:
        with pytest.raises(StopRejected, match="worker_still_running"):
            await registry.reconcile_stop(identity.operation_id, result.attempt_id, confirmed=True)
        with pytest.raises(SkillError, match="workers"):
            registry.close()
        release.set()
        await drain(registry, identity.operation_id)
        assert writes == [True, False]
        with pytest.raises(ConfirmationRequired):
            await registry.reconcile_stop(identity.operation_id, result.attempt_id)
        with pytest.raises(StopRejected, match="attempt_mismatch"):
            await registry.reconcile_stop(identity.operation_id, "wrong", confirmed=True)
        record = await registry.reconcile_stop(identity.operation_id, result.attempt_id, confirmed=True)
        assert record.state == "OUTCOME_UNKNOWN" and record.stop_state == "MOTION_STOPPED_OBSERVED"
        proof = record.stop_evidence
        assert proof["source"] == "simulation" and len(proof["samples"]) >= 2
        assert all(abs(v) <= 0.02 for sample in proof["samples"] for v in sample["velocities"])
        replay = await registry.invoke("skill.test.motion", operation=identity)
        assert not replay.ok and replay.stop_state == "MOTION_STOPPED_OBSERVED"
        assert replay.data["_execution"]["stop_state"] == "MOTION_STOPPED_OBSERVED"
        assert writes == [True, False]
        new = await registry.invoke("skill.test.motion", operation=OperationIdentity(task_id="new", operation_id="new"))
        assert new.ok and writes == [True, False, True, True]
        t = sim.read().t
        duplicate = await registry.reconcile_stop(identity.operation_id, result.attempt_id, confirmed=True)
        assert duplicate.stop_evidence == proof and sim.read().t == t
    finally:
        release.set()
        await asyncio.sleep(0)
        registry.close()


async def test_database_failure_keeps_fence_and_quarantine(tmp_path, monkeypatch):
    sim = TabletopSim()
    registry = registry_for(sim, tmp_path / "ops.sqlite3")
    identity, result, release, _ = await interrupted_motion(registry, sim)
    release.set()
    await drain(registry, identity.operation_id)
    original = registry.ledger.record_stop

    def failure(evidence):
        raise sqlite3.OperationalError("simulated disk failure")

    monkeypatch.setattr(registry.ledger, "record_stop", failure)
    with pytest.raises(sqlite3.OperationalError):
        await registry.reconcile_stop(identity.operation_id, result.attempt_id, confirmed=True)
    assert registry.get_operation(identity.operation_id).stop_state == "STOP_UNCONFIRMED"
    t = sim.read().t
    assert not sim.write(ActionCommand(sim.read().qpos)).applied
    sim.step(10)
    assert sim.read().t == t
    with pytest.raises(RuntimeError, match="stop_reconciliation_pending"):
        sim.reset()
    assert identity.operation_id in registry.ledger.quarantined()
    monkeypatch.setattr(registry.ledger, "record_stop", original)
    assert (await registry.reconcile_stop(identity.operation_id, result.attempt_id, confirmed=True)
            ).stop_state == "MOTION_STOPPED_OBSERVED"
    registry.close()


async def test_restart_reobserves_and_corrupt_proof_restores_quarantine(tmp_path):
    path = tmp_path / "ops.sqlite3"
    sim = TabletopSim()
    registry = registry_for(sim, path)
    identity, result, release, _ = await interrupted_motion(registry, sim)
    release.set()
    await drain(registry, identity.operation_id)
    old_boot = registry.ledger.boot_id
    registry.close()
    replacement = registry_for(TabletopSim(), path)
    record = await replacement.reconcile_stop(identity.operation_id, result.attempt_id, confirmed=True)
    assert record.stop_evidence["attempt_boot_id"] == old_boot
    assert record.stop_evidence["observer_boot_id"] == replacement.ledger.boot_id != old_boot
    replacement.close()
    restored = registry_for(TabletopSim(), path)
    assert restored.ledger.quarantined() == {}
    restored.close()
    with sqlite3.connect(path) as db:
        payload = json.loads(db.execute("SELECT payload_json FROM operation_events WHERE event_type='stop_observed'")
                             .fetchone()[0])
        payload["attempt_id"] = "corrupt"
        db.execute("UPDATE operation_events SET payload_json=? WHERE event_type='stop_observed'", (json.dumps(payload),))
    damaged = registry_for(TabletopSim(), path)
    assert identity.operation_id in damaged.ledger.quarantined()
    assert damaged.get_operation(identity.operation_id).stop_state == "STOP_UNCONFIRMED"
    damaged.close()


async def test_untracked_worker_is_never_recoverable_by_assumption():
    sim = TabletopSim()
    registry = registry_for(sim)
    identity, result, release, writes = await interrupted_motion(registry, sim, tracking=False)
    release.set()
    for _ in range(200):
        if len(writes) == 2:
            break
        await asyncio.sleep(0.01)
    with pytest.raises(StopRejected, match="tracking_unavailable"):
        await registry.reconcile_stop(identity.operation_id, result.attempt_id, confirmed=True)
    registry.close()


async def test_profile_change_and_guard_latch_are_not_bypassed():
    sim = TabletopSim()
    registry = registry_for(sim)
    identity, result, release, _ = await interrupted_motion(registry, sim)
    release.set()
    await drain(registry, identity.operation_id)
    controller = sim.stop_controller
    original = controller.profile
    controller.profile = original.model_copy(update={"max_velocity_rad_s": 0.03})
    with pytest.raises(StopRejected, match="profile_changed"):
        await registry.reconcile_stop(identity.operation_id, result.attempt_id, confirmed=True)
    controller.profile = original
    sim.guard.estop("test")
    record = await registry.reconcile_stop(identity.operation_id, result.attempt_id, confirmed=True)
    assert record.stop_state == "MOTION_STOPPED_OBSERVED" and sim.guard.halted
    assert not sim.write(ActionCommand(sim.read().qpos)).applied
    registry.close()


def test_only_issued_evidence_can_commit_and_fence_prevents_reset():
    sim = TabletopSim()
    controller = sim.stop_controller
    request = StopRequest(operation_id="op", attempt_id="attempt", attempt_boot_id="boot", observer_boot_id="boot",
                          robot_id=sim.spec().embodiment_id, lease_epoch=1, resources=controller.profile.resources,
                          nonce="unique", deadline_monotonic=time.monotonic() + 2)
    evidence = controller.prepare(request)
    with pytest.raises(StopRejected, match="not_issued"):
        controller.commit(evidence.model_copy(), request, lambda: None)
    with pytest.raises(RuntimeError, match="stop_reconciliation_pending"):
        sim.reset()
    old_epoch = sim.state_epoch
    sim.state_epoch = "changed"
    with pytest.raises(StopRejected, match="scene_changed"):
        controller.commit(evidence, request, lambda: None)
    sim.state_epoch = old_epoch
    controller.commit(evidence, request, lambda: None)
    sim.reset()


def test_invalid_uncontrolled_feedback_cannot_be_hidden_by_simulator_recovery():
    sim = TabletopSim()
    controller = sim.stop_controller
    request = StopRequest(operation_id="op", attempt_id="attempt", attempt_boot_id="boot", observer_boot_id="boot",
                          robot_id=sim.spec().embodiment_id, lease_epoch=1, resources=controller.profile.resources,
                          nonce="unique", deadline_monotonic=time.monotonic() + 2)
    sim.data.qpos[-1] = float("nan")
    with pytest.raises(StopRejected, match="invalid_simulation_feedback"):
        controller.prepare(request)


async def test_legacy_unknown_operation_without_tracking_metadata_stays_quarantined(tmp_path):
    sim = TabletopSim()
    path = tmp_path / "legacy.sqlite3"
    ledger = OperationLedger(path)
    identity = OperationIdentity(task_id="task", operation_id="legacy")
    record = ledger.begin(identity, sim.spec().embodiment_id, "skill.arm.home", "digest",
                          ("arm", "gripper", "workspace"), 1)
    ledger.finish("legacy", "OUTCOME_UNKNOWN", None, "STOP_UNCONFIRMED")
    ledger.close()
    registry = registry_for(sim, path)
    with pytest.raises(StopRejected, match="tracking_unavailable"):
        await registry.reconcile_stop("legacy", record.attempt_id, confirmed=True)
    assert "legacy" in registry.ledger.quarantined()
    registry.close()


async def test_late_maintenance_worker_cannot_release_or_execute_an_expired_request():
    sim = TabletopSim()
    controller = sim.stop_controller
    # 使用实际预算测试排队超时，不引入 50ms 性能要求。 / Use the real budget without asserting 50 ms physics performance.
    registry = registry_for(sim)
    identity, result, release, _ = await interrupted_motion(registry, sim)
    release.set()
    await drain(registry, identity.operation_id)
    unblock = threading.Event()
    original = controller.prepare

    def delayed(request):
        assert unblock.wait(5)
        return original(request)

    controller.prepare = delayed
    try:
        with pytest.raises(StopRejected, match="timeout"):
            await registry.reconcile_stop(identity.operation_id, result.attempt_id, confirmed=True)
        with pytest.raises(StopRejected, match="still_running"):
            await registry.reconcile_stop(identity.operation_id, result.attempt_id, confirmed=True)
        with pytest.raises(SkillError):
            registry.close()
        t = sim.read().t
        unblock.set()
        for _ in range(200):
            if registry._stop_inflight.done():
                break
            await asyncio.sleep(0.01)
        assert sim.read().t == t and not sim._stop_fence
        assert registry.get_operation(identity.operation_id).stop_state == "STOP_UNCONFIRMED"
        controller.prepare = original
        assert (await registry.reconcile_stop(identity.operation_id, result.attempt_id, confirmed=True)
                ).stop_state == "MOTION_STOPPED_OBSERVED"
    finally:
        unblock.set()
        registry.close()


async def test_maintenance_intent_must_be_durable_before_controller_action(monkeypatch):
    sim = TabletopSim()
    registry = registry_for(sim)
    identity, result, release, _ = await interrupted_motion(registry, sim)
    release.set()
    await drain(registry, identity.operation_id)
    before = sim.read().t

    def failure(*args):
        raise sqlite3.OperationalError("simulated disk failure")

    monkeypatch.setattr(registry.ledger, "note_stop_request", failure)
    with pytest.raises(sqlite3.OperationalError):
        await registry.reconcile_stop(identity.operation_id, result.attempt_id, confirmed=True)
    assert sim.read().t == before and not sim._stop_fence
    assert identity.operation_id in registry.ledger.quarantined()
    registry.close()


async def test_missing_same_boot_worker_identity_does_not_mean_drained():
    sim = TabletopSim()
    registry = registry_for(sim)
    identity, result, release, _ = await interrupted_motion(registry, sim)
    release.set()
    await drain(registry, identity.operation_id)
    permit = registry._stop_contexts.pop(identity.operation_id)
    try:
        with pytest.raises(StopRejected, match="worker_identity_unavailable"):
            await registry.reconcile_stop(identity.operation_id, result.attempt_id, confirmed=True)
        assert not sim._stop_fence and identity.operation_id in registry.ledger.quarantined()
    finally:
        registry._stop_contexts[identity.operation_id] = permit
        registry.close()


async def test_stopping_while_holding_keeps_bilateral_grip_force():
    sim = TabletopSim(seed=0)
    registry = registry_for(sim)
    register_sim_skills(registry, sim)
    assert (await registry.invoke("skill.manip.pick")).ok
    assert held_object(WorldSnapshot.from_observation(sim.read())) == "obj_cube"
    identity, result, release, _ = await interrupted_motion(registry, sim)
    release.set()
    await drain(registry, identity.operation_id)
    record = await registry.reconcile_stop(identity.operation_id, result.attempt_id, confirmed=True)
    evidence = record.stop_evidence
    assert evidence["gripper_strategy"] == "maintain_contact" and evidence["contact_objects"] == ["obj_cube"]
    assert all(min(sample["grip_forces"]) >= 0.01 for sample in evidence["samples"])
    sim.step(250)
    assert held_object(WorldSnapshot.from_observation(sim.read())) == "obj_cube"
    assert record.state == "OUTCOME_UNKNOWN"
    registry.close()
