"""Durable effect identity and recovery against the real sim. 真实仿真上的持久效果身份与恢复。"""

import json
import sqlite3

import pytest

pytest.importorskip("mujoco")

from embodied.cognition.executor import DagExecutor  # noqa: E402
from embodied.cognition.plan import Plan, PlanContext, Step, StepStatus  # noqa: E402
from embodied.contracts.effect import EffectEvidence, EffectRejected, effect_digest  # noqa: E402
from embodied.contracts.operation import OperationIdentity  # noqa: E402
from embodied.control.drivers.mujoco_sim import TabletopSim  # noqa: E402
from embodied.control.simassets import find_menagerie_model  # noqa: E402
from embodied.runtime.operations import OperationLedger  # noqa: E402
from embodied.skills.manifest import SkillManifest  # noqa: E402
from embodied.skills.registry import SkillRegistry, SkillResult  # noqa: E402
from embodied.skills.scripted.manip import register_sim_skills  # noqa: E402

try:
    find_menagerie_model()
except FileNotFoundError:
    pytest.skip("menagerie assets not fetched", allow_module_level=True)


@pytest.fixture
def env(tmp_path):
    sim = TabletopSim(seed=42)
    registry = SkillRegistry(ledger=OperationLedger(tmp_path / "ops.sqlite3"), robot_id=sim.spec().embodiment_id)
    register_sim_skills(registry, sim)
    yield sim, registry
    if not registry._closed:
        registry.close()


def register_probe(registry, *, outcome=None, verification=None):
    verification = verification or {"mode": "state_predicate", "expect": {
        "predicate": "object_near", "args": {"object": "obj_cube", "pos": [0.0, 0.0, 0.0], "radius": 1.0}}}
    manifest = SkillManifest(name="skill.test.effect", description="effect probe", resources=["arm", "gripper", "workspace"],
                             verification=verification)

    async def handler():
        return outcome or SkillResult(ok=True)

    registry.register(manifest, handler, tracked_workers=True)
    return manifest


async def invoke_probe(registry, **kwargs):
    manifest = register_probe(registry, **kwargs)
    result = await registry.invoke(manifest.name, operation=OperationIdentity(task_id="task", operation_id="op"))
    return manifest, result


async def test_effect_is_durable_and_query_does_not_resample(env):
    sim, registry = env
    manifest, result = await invoke_probe(registry)
    proof = result.data["_effect"]
    assert proof["verdict"] == "sat" and EffectEvidence.model_validate(proof).intact()
    assert registry.effect_verdict("op", result.attempt_id, proof["evidence_id"], manifest.verification) == "sat"
    t = sim.data.time
    for _ in range(2):
        record = registry.get_operation("op")
        assert record.effect_evidence == proof and record.state == "RETURNED" and record.stop_state == "NOT_REQUESTED"
    assert sim.data.time == t
    with sqlite3.connect(registry.ledger.path) as db:
        assert db.execute("SELECT count(*) FROM operation_events WHERE event_type='effect_observed'").fetchone()[0] == 1


@pytest.mark.parametrize("fault", ["receipt", "attempt", "verification", "expired", "future", "boot", "scene", "new_owner"])
async def test_old_or_mismatched_evidence_is_not_current(env, monkeypatch, fault):
    sim, registry = env
    manifest, result = await invoke_probe(registry)
    proof = result.data["_effect"]
    eid, attempt, verification = proof["evidence_id"], result.attempt_id, manifest.verification
    if fault == "receipt":
        eid = "other"
    elif fault == "attempt":
        attempt = "other"
    elif fault == "verification":
        verification = {"mode": "state_predicate"}
    elif fault in {"expired", "future"}:
        monkeypatch.setattr("embodied.skills.registry.time.monotonic",
                            lambda: proof["captured_monotonic"] + (2.1 if fault == "expired" else -0.1))
    elif fault == "boot":
        registry.ledger.boot_id = "new"
    elif fault == "scene":
        sim.reset()
    else:
        await registry.invoke("skill.arm.home")
    assert registry.effect_verdict("op", attempt, eid, verification) == "unknown"


async def test_receipt_replay_cannot_borrow_current_world_success(env):
    _, registry = env
    manifest, first = await invoke_probe(registry)
    replay = await registry.invoke(manifest.name, operation=OperationIdentity(task_id="task", operation_id="op"))
    assert replay.ok and replay.data["_operation_replay"]
    plan = Plan(steps=[Step(id="a", skill=manifest.name, operation_id="op", verification=manifest.verification),
                       Step(id="b", skill="skill.arm.home", depends_on=["a"])])
    ctx = PlanContext(task_id="task")
    results = [r async for r in DagExecutor(registry, world_fn=lambda: pytest.fail("must not resample replay")).run(plan, ctx)]
    assert results[0].data["_verify"]["verdict"] == "unknown"
    assert results[1].status == StepStatus.SKIPPED and first.data["_effect"]["verdict"] == "sat"


async def test_reconcile_unknown_requires_stop_and_never_flips_call(env):
    sim, registry = env
    manifest, result = await invoke_probe(registry, outcome=SkillResult(
        ok=False, data={"_execution": {"outcome": "OUTCOME_UNKNOWN"}},
    ))
    assert result.data["_effect"]["verdict"] == "unknown"
    with pytest.raises(EffectRejected, match="stop_unconfirmed"):
        registry.reconcile_effect("op", result.attempt_id)
    await registry.reconcile_stop("op", result.attempt_id, confirmed=True)
    t, ctrl = sim.data.time, sim.data.ctrl.copy()
    record = registry.reconcile_effect("op", result.attempt_id)
    assert record.state == "OUTCOME_UNKNOWN" and record.stop_state == "MOTION_STOPPED_OBSERVED"
    assert record.effect_evidence["verdict"] == "sat"
    assert sim.data.time == t and (sim.data.ctrl == ctrl).all()
    assert not registry._from_record(record).ok
    replay = await registry.invoke(manifest.name, operation=OperationIdentity(task_id="task", operation_id="op"))
    assert not replay.ok and replay.data["_operation_replay"]


async def test_restart_retains_history_but_cannot_certify_new_controller(env):
    _, registry = env
    _, result = await invoke_probe(registry)
    path, robot_id = registry.ledger.path, registry.robot_id
    registry.close()
    other = SkillRegistry(ledger=OperationLedger(path), robot_id=robot_id)
    register_sim_skills(other, TabletopSim())
    try:
        history = other.get_operation("op")
        assert history.effect_evidence["evidence_id"] == result.data["_effect"]["evidence_id"]
        with pytest.raises(EffectRejected, match="context_or_owner_changed"):
            other.reconcile_effect("op", result.attempt_id)
    finally:
        other.close()


async def test_persistence_failure_cannot_release_dependent_success(env, monkeypatch):
    _, registry = env
    manifest = register_probe(registry)

    def fail(evidence):
        raise sqlite3.OperationalError("injected write failure")

    monkeypatch.setattr(registry.ledger, "record_effect", fail)
    plan = Plan(steps=[Step(id="a", skill=manifest.name, verification=manifest.verification),
                       Step(id="b", skill="skill.arm.home", depends_on=["a"])])
    results = [r async for r in DagExecutor(registry).run(plan, PlanContext())]
    assert results[0].status == StepStatus.FAILED and results[1].status == StepStatus.SKIPPED


@pytest.mark.parametrize("field,value", [("verdict", "unsat"), ("attempt_id", "other"), ("digest", "invalid")])
async def test_corrupt_stored_effect_is_not_accepted(env, field, value):
    _, registry = env
    manifest, result = await invoke_probe(registry)
    proof = dict(result.data["_effect"])
    proof[field] = value
    registry.ledger._db.execute("UPDATE operation_events SET payload_json=? WHERE event_type='effect_observed'",
                               (json.dumps(proof),))
    registry.ledger._db.commit()
    assert registry.get_operation("op").effect_evidence is None
    assert registry.effect_verdict("op", result.attempt_id, proof["evidence_id"], manifest.verification) == "unknown"


async def test_recheck_rejects_later_conflicting_operation_and_wrong_attempt(env):
    sim, registry = env
    _, result = await invoke_probe(registry)
    with pytest.raises(EffectRejected, match="attempt"):
        registry.reconcile_effect("op", "wrong")
    await registry.invoke("skill.arm.home")
    t = sim.data.time
    with pytest.raises(EffectRejected, match="context_or_owner_changed"):
        registry.reconcile_effect("op", result.attempt_id)
    assert sim.data.time == t


async def test_unknown_predicate_is_persisted_as_unknown(env):
    _, registry = env
    manifest, result = await invoke_probe(registry, verification={"mode": "state_predicate", "expect": {"predicate": "future"}})
    assert result.data["_effect"]["verdict"] == "unknown"
    assert result.data["_effect"]["verification_digest"] == effect_digest(manifest.verification)


def test_color_place_binding_and_effect_recheck_do_not_hold_render_lock():
    """Keep GLFW state out of other renderer fixtures. 隔离 GLFW 状态，避免干扰其他渲染夹具。"""
    import subprocess
    import sys
    import textwrap

    code = '''
import asyncio
from embodied.control.drivers.mujoco_sim import TabletopSim
from embodied.skills.registry import SkillRegistry
from embodied.skills.scripted.manip import register_sim_skills

async def main():
    from embodied.cognition.perception import TABLETOP_OBJECTS, PerceivedSim, PerceptionPipeline
    from embodied.providers.perception import ColorBlobProvider

    sim = TabletopSim(seed=42)
    wrapped = PerceivedSim(sim, PerceptionPipeline(ColorBlobProvider(), dict(TABLETOP_OBJECTS)))
    registry = SkillRegistry(robot_id=sim.spec().embodiment_id)
    register_sim_skills(registry, wrapped)
    try:
        assert (await registry.invoke("skill.manip.pick")).ok
        placed = await registry.invoke("skill.manip.place")
        assert placed.ok and placed.data["_effect"]["verdict"] == "sat"
        t = sim.data.time
        checked = registry.reconcile_effect(placed.operation_id, placed.attempt_id)
        assert checked.effect_evidence["verdict"] == "sat" and sim.data.time == t
        assert checked.stop_context["effect_context"]["bindings"]["held_object"] == "obj_cube"
    finally:
        wrapped.close()
        registry.close()
    print("effect_recheck_passed")

asyncio.run(main())
'''
    probe = subprocess.run([sys.executable, "-c", textwrap.dedent(code)], capture_output=True, text=True, timeout=45)
    assert probe.returncode == 0, probe.stderr
    assert "effect_recheck_passed" in probe.stdout
