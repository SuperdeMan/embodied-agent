"""Recovery review rejects mixed snapshots and stale eligibility. 恢复审查拒绝混合快照与过期资格。"""

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from embodied.contracts.recovery import RecoveryReview
from embodied.runtime.operations import OperationLedger
from embodied.skills.manifest import SkillManifest
from embodied.skills.registry import SkillRegistry, SkillResult


@pytest.fixture
def review_env(tmp_path, monkeypatch):
    registry = SkillRegistry(ledger=OperationLedger(tmp_path / "ops.sqlite3"))

    async def act():
        return SkillResult(ok=True)

    registry.register(SkillManifest(name="skill.test.act", description="probe"), act)
    registry.record_task_plan({"task_id": "task", "revision": 1, "steps": [
        {"id": "a", "skill": "skill.test.act"}, {"id": "b", "skill": "skill.test.act"}]})
    limits = object()
    controller = SimpleNamespace(boot_id="controller", epoch="control", profile=SimpleNamespace(digest="profile"),
        _limits=limits, sim=SimpleNamespace(state_epoch="scene", guard=SimpleNamespace(limits=limits, halted=False, generation=0)))
    registry._execution_controller = controller
    original = registry.get_task

    def history(task_id):
        report = original(task_id)
        # 单元夹具只替换效果检查输入；真实仿真契约另有覆盖。
        # Unit fixtures replace only effect-check inputs; real simulation contracts are tested separately.
        for step in report.get("steps", []):
            step["historical_effect"] = {"evidence_id": "e-" + step["id"]}
        return report

    monkeypatch.setattr(registry, "get_task", history)
    yield registry, controller
    registry.close()


@pytest.mark.parametrize("change", ["scene", "profile", "control", "guard", "limits", "boot", "manifest", "ledger", "external"])
async def test_change_during_await_invalidates_entire_review(review_env, monkeypatch, change):
    registry, controller = review_env
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(*args):
        entered.set()
        await release.wait()
        return {"eligible": True, "verdict": "sat", "age_s": 0.0, "reason": ""}

    monkeypatch.setattr(registry, "check_effect_async", delayed)
    task = asyncio.create_task(registry.reconcile_task("task"))
    await asyncio.wait_for(entered.wait(), 2)
    old_boot = registry.ledger.boot_id
    if change == "scene":
        controller.sim.state_epoch = "changed"
    elif change == "profile":
        controller.profile.digest = "changed"
    elif change == "control":
        controller.epoch = "changed"
    elif change == "guard":
        controller.sim.guard.halted = True
    elif change == "limits":
        controller.sim.guard.limits = object()
    elif change == "boot":
        registry.ledger.boot_id = "changed"
    elif change == "manifest":
        manifest, handler = registry._skills["skill.test.act"]
        registry._skills[manifest.name] = (manifest.model_copy(update={"description": "changed"}), handler)
    elif change == "ledger":
        registry.finish_task("task")
    else:
        with sqlite3.connect(registry.ledger.path) as writer:
            writer.execute("UPDATE tasks SET updated_at=updated_at+1")
    release.set()
    report = await asyncio.wait_for(task, 2)
    review = RecoveryReview.model_validate(report["recovery_review"])
    assert not review.coherent and not review.execution_authorized
    assert review.ledger_boot_id == old_boot and "recovery_snapshot_changed" in review.blockers
    assert all(s["current_effect"]["verdict"] == "unknown" for s in report["steps"])
    assert all(not s["current_effect"]["eligible"] for s in report["steps"])
    json.dumps(report, allow_nan=False)


async def test_earlier_check_expires_while_later_check_waits(review_env, monkeypatch):
    registry, _ = review_env
    clock = [100.0]
    monkeypatch.setattr("embodied.runtime.recovery.time", SimpleNamespace(monotonic=lambda: clock[0]))

    async def check(operation_id, attempt, evidence_id, verification):
        if evidence_id == "e-b":
            clock[0] += 2.1
        return {"eligible": True, "verdict": "sat", "age_s": 0.0, "reason": ""}

    monkeypatch.setattr(registry, "check_effect_async", check)
    before = registry.ledger._db.total_changes
    report = await registry.reconcile_task("task")
    assert report["recovery_review"]["coherent"]
    assert report["steps"][0]["current_effect"]["reason"] == "effect_expired_during_review"
    assert not report["steps"][0]["current_effect"]["eligible"]
    assert registry.ledger._db.total_changes == before


@pytest.mark.parametrize("age", [float("nan"), float("inf"), -1, True, "0"])
async def test_malformed_age_never_remains_eligible(review_env, monkeypatch, age):
    registry, _ = review_env

    async def check(*args):
        return {"eligible": True, "verdict": "sat", "age_s": age, "reason": ""}

    monkeypatch.setattr(registry, "check_effect_async", check)
    report = await registry.reconcile_task("task")
    assert all(not s["current_effect"]["eligible"] for s in report["steps"])


async def test_stable_readonly_review_binds_history_but_never_authorizes(review_env, monkeypatch):
    registry, _ = review_env

    async def check(*args):
        return {"eligible": True, "verdict": "sat", "age_s": 0.0, "reason": ""}

    monkeypatch.setattr(registry, "check_effect_async", check)
    before = registry.ledger._db.total_changes
    first = await registry.reconcile_task("task")
    second = await registry.reconcile_task("task")
    review = first["recovery_review"]
    assert review["coherent"] and review["state_digest"] == second["recovery_review"]["state_digest"]
    assert review["authenticated_subject"] is None and not review["execution_authorized"]
    assert "authenticated_operator_required" in review["blockers"]
    assert registry.ledger._db.total_changes == before and not registry.ledger.has_operations()
    for mutation in ({"execution_authorized": True}, {"authenticated_subject": "operator"}, {"api_version": "future"}):
        with pytest.raises(ValidationError):
            RecoveryReview.model_validate({**review, **mutation})
    missing = await registry.reconcile_task("missing")
    assert "task_not_found" in missing["recovery_review"]["blockers"]


async def test_cancelling_review_does_not_write_or_finish_task(review_env, monkeypatch):
    registry, _ = review_env
    entered = asyncio.Event()

    async def check(*args):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(registry, "check_effect_async", check)
    before = registry.ledger._db.total_changes
    task = asyncio.create_task(registry.reconcile_task("task"))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert registry.ledger._db.total_changes == before and registry.get_task("task")["workflow"] == "ACTIVE"
