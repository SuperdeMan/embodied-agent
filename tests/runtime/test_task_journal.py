"""Task recovery is evidence review, never replay. 任务恢复先审查证据，绝不自动重放。"""

import sqlite3

import pytest

from embodied.cognition.executor import DagExecutor
from embodied.cognition.plan import Plan, PlanContext, Step, StepStatus
from embodied.contracts.operation import OperationIdentity
from embodied.contracts.task import TaskJournalError
from embodied.runtime.operations import OperationLedger
from embodied.skills.manifest import ParamSpec, SkillManifest
from embodied.skills.registry import ConfirmationRequired, SkillRegistry, SkillResult


def runtime(path=":memory:", *, dangerous=False):
    registry = SkillRegistry(ledger=OperationLedger(path))
    calls = []

    async def act(x=1):
        calls.append(x)
        return SkillResult(ok=True, data={"x": x})

    registry.register(SkillManifest(name="skill.test.act", description="probe", require_confirm=dangerous,
                                   params={"x": ParamSpec(type="integer", default=1, required=False)}), act)
    return registry, calls


def plan(**changes):
    value = {"task_id": "task", "revision": 1, "goal": "test", "steps": [
        {"id": "a", "skill": "skill.test.act", "params": {"x": 1}},
        {"id": "b", "skill": "skill.test.act", "params": {"x": 2}, "depends_on": ["a"]},
    ]}
    value.update(changes)
    return value


def identity(registry, step="a", revision=1):
    report = registry.get_task("task")
    spec = next(s for p in report["plans"] for s in p["steps"] if s["id"] == step)
    return OperationIdentity(task_id="task", operation_id=spec["operation_id"], plan_revision=revision)


async def test_plan_intent_and_finished_history_survive_restart_without_resume(tmp_path):
    path = tmp_path / "ops.sqlite3"
    first, calls = runtime(path)
    first.record_task_plan(plan())
    with pytest.raises(TaskJournalError, match="intent_missing"):
        await first.invoke("skill.test.act", {"x": 1}, operation=identity(first))
    assert not calls
    first.record_task_intent("task", 1, "a", {"x": 1})
    result = await first.invoke("skill.test.act", {"x": 1}, operation=identity(first))
    first.record_task_result("task", 1, "a", {"status": "ok", "operation_id": result.operation_id})
    first.finish_task("task")
    first.close()
    second, repeated = runtime(path)
    try:
        report = second.get_task("task")
        assert report["workflow"] == "FINISHED" and not report["same_runtime"]
        assert report["steps"][0]["call_state"] == "RETURNED"
        assert report["steps"][1]["call_state"] == "NOT_DISPATCHED"
        assert not report["automatic_resume_allowed"]
        with pytest.raises(TaskJournalError, match="recovery_review"):
            second.record_task_intent("task", 1, "b", {"x": 2})
        assert (await second.invoke("skill.test.act", {"x": 1}, operation=identity(second))).ok
        assert repeated == []
    finally:
        second.close()


async def test_unfinished_task_becomes_interrupted_and_blocks_unexecuted_step(tmp_path):
    path = tmp_path / "ops.sqlite3"
    first, _ = runtime(path)
    first.record_task_plan(plan())
    first.record_task_intent("task", 1, "b", {"x": 2})
    pending = identity(first, "b")
    first.close()
    second, calls = runtime(path)
    try:
        report = second.get_task("task")
        assert report["workflow"] == "INTERRUPTED"
        assert report["steps"][1]["call_state"] == "INTENT_ONLY"
        with pytest.raises(TaskJournalError, match="recovery_review"):
            await second.invoke("skill.test.act", {"x": 2}, operation=pending)
        assert not calls
        assert len(second.list_tasks()) == 1
    finally:
        second.close()


async def test_client_success_receipt_cannot_create_an_execution_or_effect():
    registry, calls = runtime()
    registry.record_task_plan(plan())
    registry.record_task_result("task", 1, "a", {"status": "ok", "invocation_state": "RETURNED",
                                                "verify_verdict": "sat", "operation_id": "invented"})
    before = registry.ledger._db.total_changes
    report = await registry.reconcile_task("task")
    assert registry.ledger._db.total_changes == before and not calls
    step = report["steps"][0]
    assert step["reported"]["status"] == "ok" and step["call_state"] == "NOT_DISPATCHED"
    assert step["historical_effect"] is None and step["current_effect"]["verdict"] == "unknown"
    assert not report["automatic_resume_allowed"]
    registry.record_task_intent("task", 1, "b", {"x": 2})
    with pytest.raises(TaskJournalError, match="dependency_unconfirmed"):
        await registry.invoke("skill.test.act", {"x": 2}, operation=identity(registry, "b"))
    assert not calls
    registry.close()


@pytest.mark.parametrize("mutation", ["version", "cycle", "duplicate", "unknown_skill", "unknown_param", "bad_literal", "client_confirm"])
def test_bad_plan_never_writes_intent(mutation):
    registry, _ = runtime()
    payload = plan()
    if mutation == "version":
        payload["api_version"] = "unknown"
    elif mutation == "cycle":
        payload["steps"][0]["depends_on"] = ["b"]
    elif mutation == "duplicate":
        payload["steps"][1]["id"] = "a"
    elif mutation == "unknown_skill":
        payload["steps"][0]["skill"] = "skill.no.such"
    elif mutation == "unknown_param":
        payload["steps"][0]["params"]["bad"] = 1
    elif mutation == "bad_literal":
        payload["steps"][0]["params"]["x"] = True
    else:
        payload["steps"][0]["require_confirm"] = False
    with pytest.raises((ValueError, RuntimeError)):
        registry.record_task_plan(payload)
    assert registry.list_tasks() == []
    registry.close()


async def test_confirmation_remains_receiver_owned():
    registry, calls = runtime(dangerous=True)
    registry.record_task_plan(plan())
    assert registry.get_task("task")["plans"][0]["steps"][0]["manifest"]["require_confirm"]
    registry.record_task_intent("task", 1, "a", {"x": 1})
    with pytest.raises(ConfirmationRequired):
        await registry.invoke("skill.test.act", {"x": 1}, operation=identity(registry))
    assert not calls
    registry.close()


def test_revisions_and_intents_are_immutable():
    registry, _ = runtime()
    first = registry.record_task_plan(plan())
    assert registry.record_task_plan(plan()) == first
    with pytest.raises(TaskJournalError, match="revision_conflict"):
        registry.record_task_plan(plan(goal="changed"))
    registry.record_task_intent("task", 1, "a", {"x": 1})
    with pytest.raises(TaskJournalError, match="literal_param"):
        registry.record_task_intent("task", 1, "a", {"x": 2})
    with pytest.raises(TaskJournalError, match="identity_reused"):
        registry.record_task_plan(plan(revision=2))
    registry.close()


@pytest.mark.parametrize("where", ["plan", "event", "event_type", "plan_boot"])
async def test_corrupt_journal_is_not_usable(where):
    registry, calls = runtime()
    registry.record_task_plan(plan())
    registry.record_task_intent("task", 1, "a", {"x": 1})
    operation = identity(registry)
    if where == "plan":
        registry.ledger._db.execute("UPDATE task_plans SET plan_json='{}'")
    elif where == "event":
        registry.ledger._db.execute("UPDATE task_events SET payload_json='{}'")
    elif where == "event_type":
        registry.ledger._db.execute("UPDATE task_events SET event_type='task_finished'")
    else:
        registry.ledger._db.execute("UPDATE task_plans SET recorder_boot_id='forged'")
    registry.ledger._db.commit()
    assert registry.get_task("task")["workflow"] == "CORRUPT"
    with pytest.raises((ValueError, KeyError, TaskJournalError)):
        await registry.invoke("skill.test.act", operation=operation)
    assert not calls
    registry.close()


@pytest.mark.parametrize("point", ["record_task_plan", "record_task_intent", "record_task_result"])
async def test_journal_failure_cannot_dispatch_or_release_dependents(monkeypatch, point):
    registry, calls = runtime()

    def fail(*args):
        raise sqlite3.OperationalError("injected storage failure")

    monkeypatch.setattr(registry, point, fail)
    graph = Plan(steps=[Step(id="a", skill="skill.test.act", params={"x": 1}),
                       Step(id="b", skill="skill.test.act", params={"x": 2}, depends_on=["a"])])
    results = [r async for r in DagExecutor(registry, journal=True).run(graph, PlanContext())]
    assert calls == ([1] if point == "record_task_result" else [])
    assert any(r.status == StepStatus.FAILED for r in results)
    registry.close()


async def test_changed_manifest_rejects_new_dispatch():
    registry, calls = runtime()
    registry.record_task_plan(plan())
    registry.record_task_intent("task", 1, "a", {"x": 1})
    operation = identity(registry)
    manifest, handler = registry._skills["skill.test.act"]
    registry._skills["skill.test.act"] = (manifest.model_copy(update={"description": "new version"}), handler)
    with pytest.raises(TaskJournalError, match="manifest_changed"):
        await registry.invoke("skill.test.act", {"x": 1}, operation=operation)
    assert not calls
    registry.close()


async def test_resolved_parameter_refs_are_journaled_before_call():
    registry, calls = runtime()
    graph = Plan(steps=[Step(id="a", skill="skill.test.act", params={"x": 3}),
                       Step(id="b", skill="skill.test.act", params={"x": "${a.data.x}"}, depends_on=["a"])])
    ctx = PlanContext(task_id="task")
    results = [r async for r in DagExecutor(registry, journal=True).run(graph, ctx)]
    assert all(r.status == StepStatus.OK for r in results)
    report = registry.get_task("task")
    assert report["plans"][0]["steps"][1]["params"]["x"] == "${a.data.x}"
    # 去重可能引用历史调用，不能制造第二个物理尝试。
    # Dedup may reference history; it must not manufacture a second physical attempt.
    assert calls == [3]
    assert report["steps"][1]["call_state"] == "NOT_DISPATCHED"
    registry.close()


async def test_verified_alias_can_supply_a_dependency_but_cannot_skip_its_own_dependencies():
    registry, calls = runtime()
    graph = Plan(steps=[Step(id="a", skill="skill.test.act", params={"x": 1}),
                       Step(id="b", skill="skill.test.act", params={"x": 1}, depends_on=["a"]),
                       Step(id="c", skill="skill.test.act", params={"x": 2}, depends_on=["b"])])
    results = [r async for r in DagExecutor(registry, journal=True).run(graph, PlanContext(task_id="task"))]
    assert all(r.status == StepStatus.OK for r in results) and calls == [1, 2]
    registry.close()


async def test_legacy_ledger_requires_explicit_upgrade_before_planner_actions(tmp_path):
    from embodied.runtime.operations import _SCHEMA

    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(_SCHEMA)
    registry, calls = runtime(path)
    graph = Plan(steps=[Step(id="a", skill="skill.test.act")])
    results = [r async for r in DagExecutor(registry, journal=True).run(graph, PlanContext())]
    assert not calls and results[0].status == StepStatus.FAILED
    assert "task_journal_upgrade_required" in results[0].detail
    assert registry.ledger.schema_version == 1
    registry.close()


async def test_revisions_preserve_prior_dependency_and_stop_at_capacity():
    registry, calls = runtime()
    registry.record_task_plan(plan(steps=[{"id": "a", "skill": "skill.test.act"}]))
    registry.record_task_intent("task", 1, "a", {"x": 1})
    await registry.invoke("skill.test.act", {"x": 1}, operation=identity(registry))
    for revision in range(2, 9):
        registry.record_task_plan(plan(revision=revision, steps=[
            {"id": f"s{revision}", "skill": "skill.test.act", "params": {"x": revision}, "depends_on": ["a"]}]))
    registry.record_task_intent("task", 8, "s8", {"x": 8})
    assert (await registry.invoke("skill.test.act", {"x": 8}, operation=identity(registry, "s8", 8))).ok
    with pytest.raises(TaskJournalError, match="too_large"):
        registry.record_task_plan(plan(revision=9, steps=[{"id": "s9", "skill": "skill.test.act"}]))
    assert calls == [1, 8] and len(registry.get_task("task")["plans"]) == 8
    registry.close()


async def test_engine_cancellation_marks_interrupted_and_never_starts_dependent():
    import asyncio

    from embodied.cognition.engine import PlannerEngine
    from embodied.cognition.offline import ScriptedPlanProvider

    registry, calls = runtime()
    entered = asyncio.Event()

    async def wait():
        entered.set()
        await asyncio.Event().wait()

    registry.register(SkillManifest(name="skill.test.wait", description="wait"), wait)
    provider = ScriptedPlanProvider([("go", {"steps": [
        {"id": "a", "skill": "skill.test.wait"},
        {"id": "b", "skill": "skill.test.act", "depends_on": ["a"]}]} )])
    task = asyncio.create_task(PlannerEngine(provider, registry).turn("go"))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    report = registry.get_task(registry.list_tasks()[0]["task_id"])
    assert report["workflow"] == "INTERRUPTED" and not calls
    assert report["steps"][1]["call_state"] == "NOT_DISPATCHED"
    registry.close()


def test_abrupt_process_exit_recovers_intent_without_execution(tmp_path):
    import subprocess
    import sys

    path = tmp_path / "crashed.sqlite3"
    code = '''
import os,sys
from embodied.runtime.operations import OperationLedger
from embodied.skills.registry import SkillRegistry
from embodied.skills.manifest import SkillManifest
registry=SkillRegistry(ledger=OperationLedger(sys.argv[1]))
async def act(): return None
registry.register(SkillManifest(name="skill.test.act",description="probe"),act)
registry.record_task_plan({"task_id":"task","revision":1,"steps":[{"id":"a","skill":"skill.test.act"}]})
registry.record_task_intent("task",1,"a",{})
os._exit(23)
'''
    process = subprocess.run([sys.executable, "-c", code, str(path)], timeout=15, capture_output=True)
    assert process.returncode == 23, process.stderr.decode(errors="replace")
    registry, calls = runtime(path)
    try:
        report = registry.get_task("task")
        assert report["workflow"] == "INTERRUPTED" and report["steps"][0]["call_state"] == "INTENT_ONLY"
        assert not calls and not report["automatic_resume_allowed"]
    finally:
        registry.close()
