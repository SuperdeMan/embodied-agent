"""Single-step authorization, durable consumption and unchanged historic evidence. 验证单步授权、持久消费及原始证据保留。"""

import asyncio
import hashlib
import sqlite3
from contextlib import closing
from dataclasses import replace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from embodied.contracts.operator import OperatorAuthorizationRequest, OperatorConfirmation, OperatorPolicy
from embodied.hri.server import AppContext, build_app
from embodied.runtime.operations import OperationConflict, OperationLedger
from embodied.runtime.operator import OperatorDenied, OperatorPrincipal

HEADERS = {"Host": "operator.test", "Origin": "https://operator.test", "Tailscale-User-Login": "user@example.test"}


async def payload(case):
    report = await case.registry.reconcile_task(case.identity.task_id)
    return {"scope": "retry_step", "task_id": case.identity.task_id, "operation_id": case.identity.operation_id,
            "attempt_id": case.first.attempt_id, "state_digest": report["recovery_review"]["state_digest"]}


async def test_authenticated_http_retry_preserves_both_attempts_and_never_resumes_task(retry_scenario):
    c = retry_scenario
    policy = OperatorPolicy(trusted_proxy="127.0.0.1", origin="https://operator.test", subjects={"user@example.test"},
                            scopes={"retry_step"})
    ctx = AppContext(c.sim, c.registry, lambda _: None, None, None, operator_policy=policy)
    async with TestClient(TestServer(build_app(ctx))) as client:
        url = "/api/operator/authorizations"
        denied = await client.post(url, headers=HEADERS, json=await payload(c))
        assert denied.status == 409 and len(c.calls) == 1
        await c.reconcile()
        request = await client.post(url, headers=HEADERS, json=await payload(c))
        issued = await request.json()
        assert request.status == 200, issued
        raw = issued["challenge_id"]
        stored = c.ledger._db.execute("SELECT grant_digest FROM recovery_grants").fetchone()[0]
        assert stored == hashlib.sha256(raw.encode()).hexdigest() and raw not in "\n".join(c.ledger._db.iterdump())
        confirmation = {"challenge_id": raw, "confirmed": True}
        confirmed = await asyncio.gather(*(client.post("/api/operator/confirmations", headers=HEADERS,
                                                        json=confirmation) for _ in range(2)))
        assert sorted(x.status for x in confirmed) == [200, 409]
        result = await next(x for x in confirmed if x.status == 200).json()
        assert result["status"] == "attempted" and result["invocation_state"] == "RETURNED" and result["effect_verdict"] == "sat"
        assert not result["automatic_resume_allowed"] and not result["task_execution_authorized"]
        replay = await c.registry.invoke("skill.manip.pick", operation=c.identity)
        assert replay.attempt_id == result["attempt_id"] and len(c.calls) == 2
        old = c.ledger.get(c.identity.operation_id, c.first.attempt_id)
        assert old.state == "OUTCOME_UNKNOWN" and old.effect_evidence["verdict"] == "unsat"
        report = c.registry.get_task(c.identity.task_id)
        assert report["workflow"] == "INTERRUPTED" and [a["attempt_no"] for a in report["steps"][0]["attempts"]] == [1, 2]
        with pytest.raises(OperationConflict, match="completion_identity_mismatch"):
            c.ledger.finish(c.identity.operation_id, "FAILED", {}, "NOT_REQUESTED", attempt_id=c.first.attempt_id)


@pytest.mark.parametrize("fault", ["declined", "guard_changed", "storage", "supervision", "effect_unknown", "expired"])
async def test_refused_retry_produces_no_additional_attempt(retry_scenario, monkeypatch, fault):
    c = retry_scenario
    await c.reconcile()
    authority = c.registry.step_retries
    principal = OperatorPrincipal("1" * 64, "2" * 64, ("retry_step",))
    offer = await authority.issue(principal, OperatorAuthorizationRequest(**await payload(c)))
    answer = OperatorConfirmation(challenge_id=offer["challenge_id"], confirmed=fault != "declined")
    if fault == "expired":
        key = hashlib.sha256(answer.challenge_id.encode()).hexdigest()
        authority._pending[key] = replace(authority._pending[key], deadline=0)
    if fault == "guard_changed":
        c.sim.guard.estop("test")
        c.sim.guard.reset()
    if fault == "storage":
        c.ledger._db.execute("""CREATE TEMP TRIGGER fail_retry BEFORE UPDATE OF state ON recovery_grants
                    WHEN NEW.state='CONSUMED' BEGIN SELECT RAISE(ABORT,'storage fault'); END""")
    if fault == "effect_unknown":
        observer = c.registry._effect_observer
        capture = observer.capture
        monkeypatch.setattr(observer, "capture", lambda record, boot: capture(record, boot, unavailable="missing observation"))
    if fault == "declined":
        assert (await authority.consume(principal, answer))["status"] == "declined"
    else:
        with pytest.raises((OperatorDenied, sqlite3.DatabaseError)):
            await authority.consume(principal, answer, supervision_ok=lambda: fault != "supervision")
    assert c.ledger._db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 1 and len(c.calls) == 1
    with pytest.raises(OperatorDenied):
        await authority.consume(principal, answer)


async def test_restart_at_committed_intent_recovers_new_attempt_without_reexecution(retry_scenario, tmp_path, monkeypatch):
    c = retry_scenario
    await c.reconcile()
    authority = c.registry.step_retries
    principal = OperatorPrincipal("1" * 64, "2" * 64, ("retry_step",))
    offer = await authority.issue(principal, OperatorAuthorizationRequest(**await payload(c)))
    confirmation = OperatorConfirmation(challenge_id=offer["challenge_id"], confirmed=True)
    with pytest.raises(OperatorDenied, match="unavailable"):
        await authority.consume(replace(principal, subject_hash="3" * 64), confirmation)
    snapshot = tmp_path / "after-intent.sqlite3"
    original = c.registry._execute

    async def capture_committed_intent(*args, **kwargs):
        with closing(sqlite3.connect(snapshot)) as copy:
            c.ledger._db.backup(copy)
        return await original(*args, **kwargs)

    monkeypatch.setattr(c.registry, "_execute", capture_committed_intent)
    result = await authority.consume(principal, confirmation)
    restarted = OperationLedger(snapshot)
    try:
        latest = restarted.get(c.identity.operation_id)
        assert latest.attempt_id == result["attempt_id"] and latest.state == "OUTCOME_UNKNOWN"
        assert latest.stop_state == "STOP_UNCONFIRMED" and c.identity.operation_id in restarted.quarantined()
        previous = restarted.get(c.identity.operation_id, c.first.attempt_id)
        assert previous.state == "OUTCOME_UNKNOWN" and previous.stop_state == "MOTION_STOPPED_OBSERVED"
        assert restarted._db.execute("SELECT state,new_attempt_id FROM recovery_grants").fetchone()[:] == (
            "CONSUMED", latest.attempt_id)
        assert restarted._db.execute("SELECT count(*) FROM attempts").fetchone()[0] == 2
    finally:
        restarted.close()
