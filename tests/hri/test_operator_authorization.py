"""Authenticated, audited effect maintenance over HTTP. HTTP 上经过认证和审计的效果维护。"""

import hashlib
import json
import sqlite3

import pytest
from aiohttp.test_utils import TestClient, TestServer
from multidict import CIMultiDict

pytest.importorskip("mujoco")

from embodied.contracts.operation import OperationIdentity  # noqa: E402
from embodied.contracts.operator import OperatorPolicy  # noqa: E402
from embodied.control.drivers.mujoco_sim import TabletopSim  # noqa: E402
from embodied.control.simassets import find_menagerie_model  # noqa: E402
from embodied.hri.server import AppContext, build_app  # noqa: E402
from embodied.runtime.operator import OperatorAuthority, OperatorDenied  # noqa: E402
from embodied.skills.manifest import SkillManifest  # noqa: E402
from embodied.skills.registry import SkillRegistry, SkillResult  # noqa: E402
from embodied.skills.scripted.manip import register_sim_skills  # noqa: E402

try:
    find_menagerie_model()
except FileNotFoundError:
    pytest.skip("menagerie assets not fetched", allow_module_level=True)

HEADERS = {"Host": "operator.test", "Origin": "https://operator.test", "Tailscale-User-Login": "user@example.test"}


@pytest.fixture
async def env():
    sim = TabletopSim()
    registry = SkillRegistry(robot_id=sim.spec().embodiment_id)
    register_sim_skills(registry, sim)
    calls = []

    async def probe():
        calls.append(1)
        return SkillResult(ok=True)

    manifest = SkillManifest(name="skill.test.probe", description="probe", resources=["arm", "gripper", "workspace"],
        verification={"mode": "state_predicate", "expect": {"predicate": "object_near", "args": {
            "object": "obj_cube", "pos": [0., 0., 0.], "radius": 1.0}}})
    registry.register(manifest, probe)
    registry.record_task_plan({"task_id": "task", "revision": 1, "steps": [{"id": "a", "skill": manifest.name}]})
    registry.record_task_intent("task", 1, "a", {})
    oid = registry.get_task("task")["steps"][0]["operation_id"]
    result = await registry.invoke(manifest.name, operation=OperationIdentity(task_id="task", operation_id=oid))
    assert result.ok
    policy = OperatorPolicy(trusted_proxy="127.0.0.1", origin="https://operator.test",
                            subjects={"user@example.test", "other@example.test"})
    ctx = AppContext(sim, registry, lambda _: None, None, None, operator_policy=policy)
    client = TestClient(TestServer(build_app(ctx)))
    await client.start_server()
    yield client, ctx, result, calls
    await client.close()
    registry.close()
    sim.close()


async def payload(env):
    client, _, result, _ = env
    report = await (await client.get("/api/tasks/task?assess=1")).json()
    return {"scope": "reconcile_effect", "task_id": "task", "operation_id": result.operation_id,
            "attempt_id": result.attempt_id, "state_digest": report["recovery_review"]["state_digest"]}


async def issue(env):
    response = await env[0].post("/api/operator/authorizations", json=await payload(env), headers=HEADERS)
    body = await response.json()
    assert response.status == 200, body
    return body["challenge_id"]


def events(registry):
    return [(row[0], json.loads(row[1])) for row in registry.ledger._db.execute(
        "SELECT event_type,payload_json FROM operation_events ORDER BY rowid")]


async def test_operator_identity_confirmation_and_once_only_audit(env):
    client, ctx, result, calls = env
    identity = await (await client.get("/api/operator/identity", headers=HEADERS)).json()
    subject = hashlib.sha256(b"user@example.test").hexdigest()
    assert identity["subject_hash"] == subject and identity["scopes"] == ["reconcile_effect"]
    challenge = await issue(env)
    t = ctx.sim.data.time
    response = await client.post("/api/operator/confirmations", json={"challenge_id": challenge, "confirmed": True}, headers=HEADERS)
    body = await response.json()
    assert response.status == 200 and body["status"] == "completed" and not body["task_execution_authorized"]
    assert calls == [1] and ctx.sim.data.time == t
    history = events(ctx.registry)
    assert [kind for kind, _ in history][-4:] == ["operator_authorization_issued", "operator_authorization_consumed",
                                                "effect_observed", "operator_authorization_finished"]
    assert history[-1][1]["subject_hash"] == subject and history[-1][1]["effect_evidence_id"] == body["effect_evidence_id"]
    before = ctx.registry.ledger._db.total_changes
    repeated = await client.post("/api/operator/confirmations", json={"challenge_id": challenge, "confirmed": True}, headers=HEADERS)
    assert repeated.status == 409 and ctx.registry.ledger._db.total_changes == before
    assert ctx.registry.get_operation(result.operation_id).state == "RETURNED"


@pytest.mark.parametrize("fault", ["missing", "spoofed", "origin", "host", "duplicate", "underscore"])
async def test_identity_and_audience_fail_closed_without_audit(env, fault):
    client, ctx, _, _ = env
    headers = CIMultiDict(HEADERS)
    if fault == "missing":
        del headers["Tailscale-User-Login"]
    elif fault == "spoofed":
        headers["Tailscale-User-Login"] = "admin@example.test"
    elif fault == "origin":
        headers["Origin"] = "https://evil.test"
    elif fault == "host":
        headers["Host"] = "evil.test"
    elif fault == "duplicate":
        headers.add("Tailscale-User-Login", "user@example.test")
    else:
        headers["Tailscale_User_Login"] = "user@example.test"
    before = ctx.registry.ledger._db.total_changes
    response = await client.post("/api/operator/authorizations", json=await payload(env), headers=headers)
    assert response.status == 403 and ctx.registry.ledger._db.total_changes == before


def test_direct_peer_cannot_forge_proxy_identity():
    authority = OperatorAuthority(None, OperatorPolicy(trusted_proxy="172.30.0.1", origin="https://operator.test",
                                                       subjects={"user@example.test"}))
    headers = CIMultiDict(HEADERS)
    headers["X-Forwarded-For"] = "172.30.0.1"
    for peer in ("127.0.0.1", "172.30.0.2", "100.64.0.1", None):
        with pytest.raises(OperatorDenied, match="untrusted_proxy"):
            authority.authenticate(peer, headers, mutation=True)


@pytest.mark.parametrize("fault", ["scope", "principal", "attempt", "snapshot"])
async def test_request_cannot_expand_authority_or_change_binding(env, fault):
    client, ctx, _, _ = env
    body = await payload(env)
    if fault == "scope":
        body["scope"] = "resume"
    elif fault == "principal":
        body["subject"] = "admin"
    elif fault == "attempt":
        body["attempt_id"] = "wrong"
    else:
        body["state_digest"] = "0" * 64
    before = ctx.registry.ledger._db.total_changes
    response = await client.post("/api/operator/authorizations", json=body, headers=HEADERS)
    assert response.status in (400, 409) and ctx.registry.ledger._db.total_changes == before


@pytest.mark.parametrize("fault", ["expire", "scene", "guard_cycle", "restart", "audit_write", "audit_slow", "audit_context"])
async def test_stale_or_failed_consumption_never_rechecks(env, monkeypatch, fault):
    client, ctx, _, calls = env
    challenge = await issue(env)
    if fault == "expire":
        now = ctx.operator._clock()
        ctx.operator._clock = lambda: now + 31
    elif fault == "scene":
        ctx.sim.reset()
    elif fault == "guard_cycle":
        ctx.sim.guard.estop("test")
        ctx.sim.guard.reset()
    elif fault == "restart":
        ctx.operator = OperatorAuthority(ctx.registry, ctx.operator.policy)
    elif fault == "audit_write":
        original = ctx.registry.ledger._event

        def fail(operation, attempt, kind, body):
            if kind == "operator_authorization_consumed":
                raise sqlite3.OperationalError("injected disk failure")
            original(operation, attempt, kind, body)

        monkeypatch.setattr(ctx.registry.ledger, "_event", fail)
    else:
        audit = ctx.operator._audit
        now = ctx.operator._clock()

        def changed(record, kind, body):
            audit(record, kind, body)
            if kind == "operator_authorization_consumed":
                if fault == "audit_slow":
                    ctx.operator._clock = lambda: now + 31
                else:
                    ctx.sim.guard.estop("audit race")
                    ctx.sim.guard.reset()

        monkeypatch.setattr(ctx.operator, "_audit", changed)
    before = sum(kind == "effect_observed" for kind, _ in events(ctx.registry))
    response = await client.post("/api/operator/confirmations", json={"challenge_id": challenge, "confirmed": True}, headers=HEADERS)
    assert response.status in (409, 503)
    assert calls == [1] and sum(kind == "effect_observed" for kind, _ in events(ctx.registry)) == before
    assert (await client.post("/api/operator/confirmations", json={"challenge_id": challenge, "confirmed": True}, headers=HEADERS)).status == 409


async def test_another_subject_cannot_consume_and_decline_never_rechecks(env):
    client, ctx, _, _ = env
    challenge = await issue(env)
    other = dict(HEADERS, **{"Tailscale-User-Login": "other@example.test"})
    response = await client.post("/api/operator/confirmations", json={"challenge_id": challenge, "confirmed": True}, headers=other)
    assert response.status == 409
    response = await client.post("/api/operator/confirmations", json={"challenge_id": challenge, "confirmed": False}, headers=HEADERS)
    assert response.status == 200 and (await response.json())["status"] == "declined"
    assert sum(kind == "effect_observed" for kind, _ in events(ctx.registry)) == 1


@pytest.mark.parametrize("confirmed", ["true", 1, None])
async def test_confirmation_is_strict_boolean(env, confirmed):
    challenge = await issue(env)
    response = await env[0].post("/api/operator/confirmations", json={"challenge_id": challenge, "confirmed": confirmed}, headers=HEADERS)
    assert response.status == 400
    assert sum(kind == "effect_observed" for kind, _ in events(env[1].registry)) == 1


@pytest.mark.parametrize("point", ["operator_authorization_issued", "operator_authorization_finished"])
async def test_audit_failure_is_not_success_or_reusable_authority(env, monkeypatch, point):
    client, ctx, _, calls = env
    original = ctx.registry.ledger._event

    def failed(operation, attempt, kind, body):
        if kind == point:
            raise sqlite3.OperationalError("audit failure")
        return original(operation, attempt, kind, body)

    if point.endswith("issued"):
        monkeypatch.setattr(ctx.registry.ledger, "_event", failed)
        response = await client.post("/api/operator/authorizations", json=await payload(env), headers=HEADERS)
        assert response.status == 503 and not ctx.operator._pending
        assert sum(kind == "effect_observed" for kind, _ in events(ctx.registry)) == 1
    else:
        challenge = await issue(env)
        monkeypatch.setattr(ctx.registry.ledger, "_event", failed)
        response = await client.post("/api/operator/confirmations", json={"challenge_id": challenge, "confirmed": True}, headers=HEADERS)
        assert response.status == 503 and not ctx.operator._pending
        assert sum(kind == "effect_observed" for kind, _ in events(ctx.registry)) == 2
        assert any(kind == "operator_authorization_consumed" for kind, _ in events(ctx.registry))
        assert (await client.post("/api/operator/confirmations", json={"challenge_id": challenge, "confirmed": True}, headers=HEADERS)).status == 409
    assert calls == [1]
