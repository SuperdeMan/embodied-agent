"""Blocking observer faults at actual runtime boundaries. 实际运行时边界上的观察阻塞故障。"""

import asyncio
import threading
import time

import pytest

pytest.importorskip("mujoco")

from embodied.contracts.operation import OperationIdentity  # noqa: E402
from embodied.contracts.stopping import StopRejected  # noqa: E402
from embodied.control.drivers.mujoco_sim import TabletopSim  # noqa: E402
from embodied.control.hal import ActionCommand  # noqa: E402
from embodied.control.simassets import find_menagerie_model  # noqa: E402
from embodied.runtime.operations import OperationLedger  # noqa: E402
from embodied.skills.manifest import SkillManifest  # noqa: E402
from embodied.skills.registry import SkillError, SkillRegistry, SkillResult  # noqa: E402
from embodied.skills.scripted.manip import register_sim_skills  # noqa: E402

try:
    find_menagerie_model()
except FileNotFoundError:
    pytest.skip("menagerie assets not fetched", allow_module_level=True)


@pytest.fixture
def env(tmp_path):
    sim = TabletopSim(seed=42)
    registry = SkillRegistry(ledger=OperationLedger(tmp_path / "ops.sqlite3"), robot_id=sim.spec().embodiment_id,
                             observation_timeout_s=0.05)
    register_sim_skills(registry, sim)
    yield sim, registry
    registry.close()


async def drain(registry):
    for _ in range(200):
        if not registry.observation_pending:
            return
        await asyncio.sleep(0.005)
    raise AssertionError("observer did not drain")


def probe(registry, *, bind=False):
    calls = []

    async def handler():
        calls.append(1)
        return SkillResult(ok=True)

    manifest = SkillManifest(name="skill.test.observe", description="observation fault probe",
                             resources=["arm", "gripper", "workspace"], verification={"mode": "state_predicate",
                             "expect": {"predicate": "object_near", "args": {
                                 "object": "$input:held_object" if bind else "obj_cube",
                                 "pos": [0.0, 0.0, 0.0], "radius": 1.0}}})
    registry.register(manifest, handler, tracked_workers=True)
    return manifest, calls


def block(monkeypatch, observer, method):
    entered, release = threading.Event(), threading.Event()
    original = getattr(observer, method)

    def blocked(*args, **kwargs):
        if kwargs.get("unavailable"):
            return original(*args, **kwargs)
        entered.set()
        assert release.wait(2)
        return original(*args, **kwargs)

    monkeypatch.setattr(observer, method, blocked)
    return entered, release


async def test_effect_timeout_preserves_call_and_never_commits_late_sat(env, monkeypatch):
    sim, registry = env
    manifest, calls = probe(registry)
    _, release = block(monkeypatch, registry._effect_observer, "capture")
    heartbeat = asyncio.create_task(asyncio.sleep(0.01))
    try:
        started = time.monotonic()
        result = await registry.invoke(manifest.name, operation=OperationIdentity(task_id="task", operation_id="op"))
        assert heartbeat.done() and time.monotonic() - started < 0.5
        assert result.ok and result.invocation_state == "RETURNED" and calls == [1]
        proof = result.data["_effect"]
        assert proof["verdict"] == "unknown" and proof["reason"] == "observation_timeout"
        assert registry.observation_pending and registry.get_operation("op").state == "RETURNED"
        replay = await registry.invoke(manifest.name, operation=OperationIdentity(task_id="task", operation_id="op"))
        assert replay.ok and replay.data["_operation_replay"] and calls == [1]
        rejected = await registry.invoke("skill.arm.home")
        assert rejected.invocation_state == "REJECTED" and rejected.detail == "observation_worker_busy"
        with pytest.raises(RuntimeError, match="observation_worker_busy"):
            sim.reset()
        t = sim.data.time
        sim.step(50)
        assert sim.data.time == t
        with pytest.raises(SkillError, match="workers"):
            registry.close()
        with pytest.raises(StopRejected, match="observation_worker"):
            await registry.reconcile_stop("op", result.attempt_id, confirmed=True)
    finally:
        release.set()
        await drain(registry)
    assert registry.get_operation("op").effect_evidence == proof
    assert (await registry.invoke("skill.arm.home")).ok


async def test_binding_timeout_cannot_run_handler_or_commit_late_binding(env, monkeypatch):
    _, registry = env
    manifest, calls = probe(registry, bind=True)
    _, release = block(monkeypatch, registry._effect_observer, "bind_context")
    try:
        result = await registry.invoke(manifest.name, operation=OperationIdentity(task_id="task", operation_id="op"))
        assert result.invocation_state == "FAILED" and not calls
        assert registry.get_operation("op").stop_context["effect_context"]["binding_pending"]
    finally:
        release.set()
        await drain(registry)
    assert registry.get_operation("op").stop_context["effect_context"]["binding_pending"]
    assert registry.ledger._db.execute("SELECT count(*) FROM operation_events WHERE event_type='effect_bound'").fetchone()[0] == 0


@pytest.mark.parametrize("stage", ["bind_context", "capture"])
async def test_cancel_wait_retains_actual_worker_and_drops_commit(env, monkeypatch, stage):
    _, registry = env
    registry._observations.timeout_s = 1.0
    manifest, calls = probe(registry, bind=stage == "bind_context")
    entered, release = block(monkeypatch, registry._effect_observer, stage)
    task = asyncio.create_task(registry.invoke(manifest.name, operation=OperationIdentity(task_id="task", operation_id="op")))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        record = registry.cancel_operation("op")
        with pytest.raises(asyncio.CancelledError):
            await task
        assert registry.observation_pending
        assert record.state == ("OUTCOME_UNKNOWN" if stage == "bind_context" else "RETURNED")
    finally:
        release.set()
        await drain(registry)
    record = registry.get_operation("op")
    if stage == "bind_context":
        assert not calls and record.stop_state == "STOP_UNCONFIRMED"
        await registry.reconcile_stop("op", record.attempt_id, confirmed=True)
    else:
        assert record.effect_evidence["verdict"] == "unknown"
        assert record.effect_evidence["reason"] == "effect_observation_cancelled"
    assert (await registry.invoke("skill.arm.home")).ok


async def test_recheck_timeout_replaces_prior_sat_with_unknown_without_releasing_late_sat(env, monkeypatch):
    _, registry = env
    manifest, _ = probe(registry)
    result = await registry.invoke(manifest.name)
    assert result.data["_effect"]["verdict"] == "sat"
    _, release = block(monkeypatch, registry._effect_observer, "capture")
    try:
        checked = await registry.reconcile_effect_async(result.operation_id, result.attempt_id)
        assert checked.state == "RETURNED" and checked.effect_evidence["verdict"] == "unknown"
        assert checked.effect_evidence["evidence_id"] != result.data["_effect"]["evidence_id"]
    finally:
        release.set()
        await drain(registry)
    assert registry.get_operation(result.operation_id).effect_evidence == checked.effect_evidence


async def test_observation_context_denies_native_actions_even_for_unbound_fixture(env):
    sim, registry = env
    raw = TabletopSim()
    for body in (sim, raw):
        ctrl, t = body.data.ctrl.copy(), body.data.time

        def adversarial():
            assert not body.write(ActionCommand(body.read().qpos)).applied
            body.step(10)
            with pytest.raises(RuntimeError, match="read_only"):
                body.reset()

        await registry.observe(adversarial)
        assert (body.data.ctrl == ctrl).all() and body.data.time == t


async def test_binding_and_effect_ledger_commits_stay_on_owner_thread(env, monkeypatch):
    _, registry = env
    owner = threading.get_ident()
    events = []
    original = registry.ledger._event

    def event(*args):
        assert threading.get_ident() == owner
        events.append(args[2])
        return original(*args)

    monkeypatch.setattr(registry.ledger, "_event", event)
    manifest, _ = probe(registry, bind=True)
    result = await registry.invoke(manifest.name)
    assert result.ok and "effect_bound" in events and "effect_observed" in events
    await registry.reconcile_effect_async(result.operation_id, result.attempt_id)


async def test_stuck_frame_cannot_block_new_admission_on_native_lock(env, monkeypatch):
    import numpy as np

    from embodied.hri.server import AppContext
    from embodied.runtime.observation import ObservationUnavailable

    sim, registry = env
    entered, release = threading.Event(), threading.Event()

    def render(camera):
        with sim._state_lock:
            entered.set()
            assert release.wait(2)
            return np.zeros((8, 8, 3), dtype=np.uint8)

    monkeypatch.setattr(sim, "render", render)
    ctx = AppContext(sim, registry, None, None, None)
    ctx._render_timeout_s = 0.05
    frame = asyncio.create_task(ctx.render_jpeg())
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        started = time.monotonic()
        result = await registry.invoke("skill.arm.home")
        assert result.invocation_state == "REJECTED" and time.monotonic() - started < 0.3
        with pytest.raises(RuntimeError, match="observation_worker_busy"):
            sim.reset()
        assert registry.get_operation("missing") is None
        with pytest.raises(ObservationUnavailable, match="timeout"):
            await frame
    finally:
        release.set()
        await asyncio.wait_for(asyncio.shield(ctx._frame_future), 1)
        await ctx.close()
