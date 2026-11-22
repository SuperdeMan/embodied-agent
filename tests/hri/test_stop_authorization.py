"""Authenticated, audited one-use stop reconciliation over HTTP. HTTP 上经认证和审计的一次性停止对账。"""

import asyncio
import hashlib
import json
import sqlite3
import threading

import pytest
from aiohttp.test_utils import TestClient, TestServer

pytest.importorskip("mujoco")

from embodied.cognition.world_state import WorldSnapshot, held_object  # noqa: E402
from embodied.contracts.operation import OperationIdentity  # noqa: E402
from embodied.contracts.operator import OperatorPolicy  # noqa: E402
from embodied.contracts.stopping import StopRejected  # noqa: E402
from embodied.control.drivers.mujoco_sim import TabletopSim  # noqa: E402
from embodied.control.hal import ActionCommand  # noqa: E402
from embodied.control.simassets import find_menagerie_model  # noqa: E402
from embodied.hri.server import AppContext, build_app  # noqa: E402
from embodied.runtime.invocation import run_blocking  # noqa: E402
from embodied.runtime.operations import OperationLedger  # noqa: E402
from embodied.runtime.operator import OperatorAuthority  # noqa: E402
from embodied.skills.manifest import SkillManifest, TerminationSpec  # noqa: E402
from embodied.skills.registry import SkillRegistry, SkillResult  # noqa: E402
from embodied.skills.scripted.manip import register_sim_skills  # noqa: E402

try:
    find_menagerie_model()
except FileNotFoundError:
    pytest.skip("menagerie assets not fetched", allow_module_level=True)

HEADERS = {"Host": "operator.test", "Origin": "https://operator.test", "Tailscale-User-Login": "user@example.test"}


async def _drain(registry, operation_id):
    for _ in range(200):
        permit = registry._stop_contexts.get(operation_id)
        if permit is not None and permit.drained:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("worker failed to drain")


def _events(registry, operation_id=None):
    if operation_id is None:
        rows = registry.ledger._db.execute("SELECT event_type,payload_json FROM operation_events ORDER BY rowid")
    else:
        rows = registry.ledger._db.execute(
            "SELECT event_type,payload_json FROM operation_events WHERE operation_id=? ORDER BY rowid", (operation_id,),
        )
    return [(row[0], json.loads(row[1])) for row in rows]


async def _make_env(
    tmp_path, *, scopes=("reconcile_effect", "reconcile_stop"), omit_scopes=False,
    tracked=True, hold_cube=False, drain_worker=True,
):
    path = tmp_path / "ops.sqlite3"
    sim = TabletopSim(seed=0)
    registry = SkillRegistry(ledger=OperationLedger(path), robot_id=sim.spec().embodiment_id)
    register_sim_skills(registry, sim)
    if hold_cube:
        pick = await registry.invoke("skill.manip.pick")
        assert pick.ok and held_object(WorldSnapshot.from_observation(sim.read())) == "obj_cube"
    entered, release = threading.Event(), threading.Event()
    writes = []

    def worker():
        q = list(sim.read().qpos)
        q[0] += 0.08
        cmd = ActionCommand(tuple(q))
        writes.append(sim.write(cmd).applied)
        sim.step(20)
        entered.set()
        if not release.wait(5):
            raise RuntimeError("test worker release missing")
        writes.append(sim.write(cmd).applied)
        return SkillResult(ok=True)

    async def motion():
        return await (run_blocking(worker) if tracked else asyncio.to_thread(worker))

    manifest = SkillManifest(
        name="skill.test.motion", description="motion", resources=["arm", "gripper", "workspace"],
        termination=TerminationSpec(timeout_s=0.1),
        verification={"mode": "state_predicate", "expect": {"predicate": "object_near", "args": {
            "object": "obj_cube", "pos": [0., 0., 0.], "radius": 1.0}}},
    )
    registry.register(manifest, motion, tracked_workers=tracked)
    registry.record_task_plan({"task_id": "task-stop", "revision": 1, "steps": [{"id": "s1", "skill": manifest.name}]})
    registry.record_task_intent("task-stop", 1, "s1", {})
    oid = registry.get_task("task-stop")["steps"][0]["operation_id"]
    task = asyncio.create_task(registry.invoke(manifest.name, operation=OperationIdentity(task_id="task-stop", operation_id=oid)))
    assert await asyncio.to_thread(entered.wait, 2)
    result = await task
    assert result.invocation_state == "OUTCOME_UNKNOWN" and result.stop_state == "STOP_UNCONFIRMED"
    registry.finish_task("task-stop", interrupted=True)
    if drain_worker:
        release.set()
        if tracked:
            await _drain(registry, oid)
        else:
            for _ in range(200):
                if len(writes) == 2:
                    break
                await asyncio.sleep(0.01)
    policy_data = {"trusted_proxy": "127.0.0.1", "origin": "https://operator.test",
                   "subjects": ["user@example.test", "other@example.test"]}
    if not omit_scopes:
        policy_data["scopes"] = list(scopes)
    policy = OperatorPolicy.model_validate(policy_data)
    ctx = AppContext(sim, registry, lambda _: None, None, None, operator_policy=policy)
    client = TestClient(TestServer(build_app(ctx)))
    await client.start_server()
    return client, ctx, result, release, writes, path


async def _close_env(client, ctx, release):
    release.set()
    for permit in list(ctx.registry._stop_contexts.values()):
        for _ in range(200):
            if permit.drained:
                break
            await asyncio.sleep(0.01)
    await client.close()
    ctx.registry.close()
    ctx.sim.close()


async def _payload(client, result, *, scope="reconcile_stop", task_id="task-stop"):
    report = await (await client.get(f"/api/tasks/{task_id}?assess=1")).json()
    return {"scope": scope, "task_id": task_id, "operation_id": result.operation_id,
            "attempt_id": result.attempt_id, "state_digest": report["recovery_review"]["state_digest"]}


async def _issue(client, result, *, scope="reconcile_stop", task_id="task-stop"):
    response = await client.post("/api/operator/authorizations", json=await _payload(client, result, scope=scope, task_id=task_id),
                                 headers=HEADERS)
    body = await response.json()
    assert response.status == 200, body
    return body


async def test_default_policy_omitting_scopes_denies_stop_without_audit(tmp_path):
    client, ctx, result, release, _, _ = await _make_env(tmp_path, omit_scopes=True)
    try:
        identity = await (await client.get("/api/operator/identity", headers=HEADERS)).json()
        assert identity["scopes"] == ["reconcile_effect"] and not identity["task_execution_authorized"]
        before = ctx.registry.ledger._db.total_changes
        response = await client.post("/api/operator/authorizations", json=await _payload(client, result), headers=HEADERS)
        body = await response.json()
        assert response.status == 403 and body["error"] == "operator_scope_not_allowed"
        assert not body["task_execution_authorized"] and ctx.registry.ledger._db.total_changes == before
        assert result.operation_id in ctx.registry.ledger.quarantined()
    finally:
        await _close_env(client, ctx, release)


async def test_stop_only_policy_denies_effect_without_audit(tmp_path):
    client, ctx, result, release, _, _ = await _make_env(tmp_path, scopes=("reconcile_stop",))
    try:
        identity = await (await client.get("/api/operator/identity", headers=HEADERS)).json()
        assert identity["scopes"] == ["reconcile_stop"]
        before = ctx.registry.ledger._db.total_changes
        response = await client.post("/api/operator/authorizations",
                                     json=await _payload(client, result, scope="reconcile_effect"), headers=HEADERS)
        assert response.status == 403 and (await response.json())["error"] == "operator_scope_not_allowed"
        assert ctx.registry.ledger._db.total_changes == before
    finally:
        await _close_env(client, ctx, release)


async def test_authenticated_stop_workflow_audits_preserves_contact_and_releases_quarantine(tmp_path):
    client, ctx, result, release, writes, _ = await _make_env(tmp_path, hold_cube=True, drain_worker=False)
    try:
        # 后台工作线程未退出时拒绝签发，不写审计。 / Refuse issuance while worker thread is still running, with zero audit writes.
        before = ctx.registry.ledger._db.total_changes
        busy = await client.post("/api/operator/authorizations", json=await _payload(client, result), headers=HEADERS)
        assert busy.status == 409 and (await busy.json())["error"] == "worker_still_running"
        assert ctx.registry.ledger._db.total_changes == before

        release.set()
        await _drain(ctx.registry, result.operation_id)
        assert writes == [True, False]
        assert "maintenance.reconcile_stop" not in [m.name for m in ctx.registry.catalog()]

        # 未确认停止前，效果重查与新操作均被拒绝。 / Effect recheck and new skills stay blocked until stop is confirmed.
        blocked_effect = await client.post("/api/operator/authorizations",
                                           json=await _payload(client, result, scope="reconcile_effect"), headers=HEADERS)
        assert blocked_effect.status == 409 and (await blocked_effect.json())["error"] == "effect_stop_unconfirmed"
        blocked_skill = await ctx.registry.invoke("skill.arm.home")
        assert not blocked_skill.ok and "resource_unavailable" in blocked_skill.detail

        # 拒绝确认不推进仿真、不解除隔离。 / Declining confirmation never steps physics or clears quarantine.
        declined_grant = await _issue(client, result)
        assert declined_grant["scope"] == "reconcile_stop" and not declined_grant["task_execution_authorized"]
        sim_t = ctx.sim.data.time
        other = dict(HEADERS, **{"Tailscale-User-Login": "other@example.test"})
        assert (await client.post("/api/operator/confirmations",
                                  json={"challenge_id": declined_grant["challenge_id"], "confirmed": True}, headers=other)).status == 409
        for bad in ("true", 1, None):
            assert (await client.post("/api/operator/confirmations",
                                      json={"challenge_id": declined_grant["challenge_id"], "confirmed": bad}, headers=HEADERS)).status == 400
        declined = await client.post("/api/operator/confirmations",
                                     json={"challenge_id": declined_grant["challenge_id"], "confirmed": False}, headers=HEADERS)
        assert declined.status == 200 and (await declined.json())["status"] == "declined"
        assert ctx.sim.data.time == sim_t and result.operation_id in ctx.registry.ledger.quarantined()

        # 确认停止：完整审计序、保持夹持接触、解除隔离但原调用保持 UNKNOWN。
        # Confirmed stop: exact audit order, bilateral contact maintained, quarantine released while call remains UNKNOWN.
        grant = await _issue(client, result)
        confirm = await client.post("/api/operator/confirmations",
                                    json={"challenge_id": grant["challenge_id"], "confirmed": True}, headers=HEADERS)
        body = await confirm.json()
        assert confirm.status == 200, body
        assert body["status"] == "completed" and body["scope"] == "reconcile_stop"
        assert body["invocation_state"] == "OUTCOME_UNKNOWN" and body["stop_state"] == "MOTION_STOPPED_OBSERVED"
        assert body["gripper_strategy"] == "maintain_contact" and not body["guard_halted"]
        assert not body["task_execution_authorized"]

        history = _events(ctx.registry, result.operation_id)
        assert [kind for kind, _ in history][-5:] == [
            "operator_authorization_issued", "operator_authorization_consumed",
            "stop_reconciliation_requested", "stop_observed", "operator_authorization_finished",
        ]
        subject = hashlib.sha256(b"user@example.test").hexdigest()
        assert history[-1][1]["subject_hash"] == subject and history[-1][1]["stop_nonce"] == body["stop_nonce"]
        assert held_object(WorldSnapshot.from_observation(ctx.sim.read())) == "obj_cube"

        # 重复消费和重复签发均拒绝且不写库。 / Duplicate confirm and re-issuance fail closed with zero DB writes.
        before = ctx.registry.ledger._db.total_changes
        assert (await client.post("/api/operator/confirmations",
                                  json={"challenge_id": grant["challenge_id"], "confirmed": True}, headers=HEADERS)).status == 409
        again = await client.post("/api/operator/authorizations", json=await _payload(client, result), headers=HEADERS)
        assert again.status == 409 and (await again.json())["error"] == "stop_already_observed"
        assert ctx.registry.ledger._db.total_changes == before

        # 旧 operation_id 不重放，新显式操作放行，效果重查现在可单独授权。
        # Old operation_id never replays, new explicit operations run, and effect review can now be authorized separately.
        replay = await ctx.registry.invoke("skill.test.motion", operation=OperationIdentity(
            task_id="task-stop", operation_id=result.operation_id))
        assert not replay.ok and replay.stop_state == "MOTION_STOPPED_OBSERVED" and writes == [True, False]

        effect_grant = await _issue(client, result, scope="reconcile_effect")
        effect_res = await client.post("/api/operator/confirmations",
                                       json={"challenge_id": effect_grant["challenge_id"], "confirmed": True}, headers=HEADERS)
        assert effect_res.status == 200 and (await effect_res.json())["scope"] == "reconcile_effect"
        assert (await ctx.registry.invoke("skill.arm.home")).ok
    finally:
        await _close_env(client, ctx, release)


async def test_cross_restart_stop_authorization_succeeds_while_effect_stays_denied(tmp_path):
    client, ctx, result, release, _, path = await _make_env(tmp_path)
    old_boot = ctx.registry.ledger.boot_id
    await _close_env(client, ctx, release)

    # 重启后上下文已变更：允许受控停止对账，继续拒绝跨重启效果重查。
    # After restart, context differs: stop authorization is permitted while cross-restart effect review stays denied.
    sim = TabletopSim(seed=0)
    registry = SkillRegistry(ledger=OperationLedger(path), robot_id=sim.spec().embodiment_id)
    register_sim_skills(registry, sim)
    policy = OperatorPolicy(trusted_proxy="127.0.0.1", origin="https://operator.test",
                            subjects={"user@example.test"}, scopes={"reconcile_effect", "reconcile_stop"})
    ctx2 = AppContext(sim, registry, lambda _: None, None, None, operator_policy=policy)
    client2 = TestClient(TestServer(build_app(ctx2)))
    await client2.start_server()
    try:
        report = await (await client2.get("/api/tasks/task-stop?assess=1")).json()
        assert report["workflow"] == "INTERRUPTED" and not report["same_runtime"] and not report["context_matches"]
        assert result.operation_id in registry.ledger.quarantined()

        effect_denied = await client2.post("/api/operator/authorizations",
                                           json=await _payload(client2, result, scope="reconcile_effect"), headers=HEADERS)
        assert effect_denied.status == 409 and (await effect_denied.json())["error"] == "review_context_unavailable"

        grant = await _issue(client2, result)
        confirm = await client2.post("/api/operator/confirmations",
                                     json={"challenge_id": grant["challenge_id"], "confirmed": True}, headers=HEADERS)
        body = await confirm.json()
        assert confirm.status == 200 and body["stop_state"] == "MOTION_STOPPED_OBSERVED"
        assert body["invocation_state"] == "OUTCOME_UNKNOWN" and not body["task_execution_authorized"]
        record = registry.get_operation(result.operation_id)
        assert record.stop_evidence["attempt_boot_id"] == old_boot
        assert record.stop_evidence["observer_boot_id"] == registry.ledger.boot_id != old_boot
        assert registry.ledger.quarantined() == {}

        # 解除隔离后，跨重启场景仍不允许补查旧操作效果。
        # Even after quarantine release, cross-restart scene state cannot be claimed for effect review.
        effect_still_denied = await client2.post("/api/operator/authorizations",
                                                 json=await _payload(client2, result, scope="reconcile_effect"), headers=HEADERS)
        assert effect_still_denied.status == 409 and (await effect_still_denied.json())["error"] == "review_context_unavailable"
        assert (await registry.invoke("skill.arm.home")).ok
    finally:
        await client2.close()
        registry.close()
        sim.close()


async def test_guard_estop_allows_stop_reconciliation_without_clearing_latch(tmp_path):
    client, ctx, result, release, _, _ = await _make_env(tmp_path)
    try:
        ctx.sim.guard.estop("emergency")
        gen_before = ctx.sim.guard.generation
        effect_denied = await client.post("/api/operator/authorizations",
                                          json=await _payload(client, result, scope="reconcile_effect"), headers=HEADERS)
        assert effect_denied.status == 409

        grant = await _issue(client, result)
        confirm = await client.post("/api/operator/confirmations",
                                    json={"challenge_id": grant["challenge_id"], "confirmed": True}, headers=HEADERS)
        body = await confirm.json()
        assert confirm.status == 200 and body["stop_state"] == "MOTION_STOPPED_OBSERVED"
        assert body["guard_halted"] is True and ctx.sim.guard.halted is True and ctx.sim.guard.generation == gen_before
        assert ctx.registry.ledger.quarantined() == {}
        assert not ctx.sim.write(ActionCommand(ctx.sim.read().qpos)).applied
    finally:
        await _close_env(client, ctx, release)


@pytest.mark.parametrize("fault", [
    "expire", "scene_reset", "guard_cycle", "stop_profile", "stop_fence",
    "restart", "audit_write", "audit_slow", "audit_context", "superseded",
])
async def test_stale_changed_or_failed_stop_confirmation_never_runs_controller(tmp_path, monkeypatch, fault):
    client, ctx, result, release, _, _ = await _make_env(tmp_path)
    try:
        grant = await _issue(client, result)
        challenge = grant["challenge_id"]
        called = []
        original_prepare = ctx.sim.stop_controller.prepare

        def spy_prepare(req):
            called.append(req)
            return original_prepare(req)

        monkeypatch.setattr(ctx.sim.stop_controller, "prepare", spy_prepare)
        if fault == "expire":
            now = ctx.operator._clock()
            ctx.operator._clock = lambda: now + 31
        elif fault == "scene_reset":
            ctx.sim.state_epoch = "changed-scene"
        elif fault == "guard_cycle":
            ctx.sim.guard.estop("cycle")
            ctx.sim.guard.reset()
        elif fault == "stop_profile":
            ctx.sim.stop_controller.profile = ctx.sim.stop_controller.profile.model_copy(
                update={"max_velocity_rad_s": 0.03},
            )
        elif fault == "stop_fence":
            ctx.sim._stop_fence = "external-fence"
        elif fault == "restart":
            ctx.operator = OperatorAuthority(ctx.registry, ctx.operator.policy)
        elif fault == "audit_write":
            original = ctx.registry.ledger._event

            def fail(op, attempt, kind, body):
                if kind == "operator_authorization_consumed":
                    raise sqlite3.OperationalError("injected disk failure")
                return original(op, attempt, kind, body)

            monkeypatch.setattr(ctx.registry.ledger, "_event", fail)
        elif fault == "superseded":
            await _issue(client, result)
        else:
            audit = ctx.operator._audit
            now = ctx.operator._clock()

            def race(record, kind, body):
                audit(record, kind, body)
                if kind == "operator_authorization_consumed":
                    if fault == "audit_slow":
                        ctx.operator._clock = lambda: now + 31
                    else:
                        ctx.sim.guard.estop("audit race")
                        ctx.sim.guard.reset()

            monkeypatch.setattr(ctx.operator, "_audit", race)

        response = await client.post("/api/operator/confirmations",
                                     json={"challenge_id": challenge, "confirmed": True}, headers=HEADERS)
        assert response.status in (409, 503)
        assert not called and ctx.registry.get_operation(result.operation_id).stop_state == "STOP_UNCONFIRMED"
        assert result.operation_id in ctx.registry.ledger.quarantined()
        assert (await client.post("/api/operator/confirmations",
                                  json={"challenge_id": challenge, "confirmed": True}, headers=HEADERS)).status == 409
    finally:
        ctx.sim._stop_fence = None
        await _close_env(client, ctx, release)


@pytest.mark.parametrize("kind", ["returned", "untracked", "profile_changed", "other_fence", "tampered_guard", "observation_busy"])
async def test_ineligible_stop_targets_fail_closed_without_audit(tmp_path, kind):
    client, ctx, result, release, _, _ = await _make_env(tmp_path, tracked=(kind != "untracked"))
    busy = [False]
    ctx.registry.add_read_barrier(lambda: busy[0])
    try:
        target_result = result
        task_id = "task-stop"
        if kind == "returned":
            # 先解除原操作隔离，再创建正常完成的 RETURNED 操作测试拒绝逻辑。
            # Release initial quarantine first, then create a clean RETURNED operation to verify refusal.
            await ctx.registry.reconcile_stop(result.operation_id, result.attempt_id, confirmed=True)
            ctx.registry.record_task_plan({"task_id": "task-ok", "revision": 1, "steps": [{"id": "h", "skill": "skill.arm.home"}]})
            ctx.registry.record_task_intent("task-ok", 1, "h", {})
            ok_id = ctx.registry.get_task("task-ok")["steps"][0]["operation_id"]
            target_result = await ctx.registry.invoke("skill.arm.home", operation=OperationIdentity(task_id="task-ok", operation_id=ok_id))
            task_id = "task-ok"
        elif kind == "profile_changed":
            ctx.sim.stop_controller.profile = ctx.sim.stop_controller.profile.model_copy(
                update={"max_velocity_rad_s": 0.03},
            )
        elif kind == "other_fence":
            ctx.sim._stop_fence = "foreign"
            ctx.sim.stop_controller._owner = "other-op"
        elif kind == "tampered_guard":
            ctx.sim.guard.limits = object()
        elif kind == "observation_busy":
            busy[0] = True

        before = ctx.registry.ledger._db.total_changes
        response = await client.post("/api/operator/authorizations",
                                     json=await _payload(client, target_result, task_id=task_id), headers=HEADERS)
        assert response.status == 409 and ctx.registry.ledger._db.total_changes == before
    finally:
        busy[0] = False
        ctx.sim._stop_fence = None
        await _close_env(client, ctx, release)


async def test_legacy_unknown_task_without_plan_is_denied_without_audit(tmp_path):
    sim = TabletopSim(seed=0)
    registry = SkillRegistry(ledger=OperationLedger(tmp_path / "legacy.sqlite3"), robot_id=sim.spec().embodiment_id)
    register_sim_skills(registry, sim)
    # 不记录任务计划直接落账的旧任务属于 LEGACY，不能经 HTTP 停止授权绕过审查绑定。
    # A task without a recorded plan is LEGACY and cannot bypass review binding via HTTP stop authorization.
    identity = OperationIdentity(task_id="legacy-task", operation_id="legacy-op")
    record = registry.ledger.begin(identity, sim.spec().embodiment_id, "skill.arm.home", "digest",
                                   ("arm", "gripper", "workspace"), 1,
                                   stop_context={"tracked_workers": True,
                                                 "stop_profile": sim.stop_controller.profile.model_dump(mode="json")})
    registry.ledger.finish("legacy-op", "OUTCOME_UNKNOWN", None, "STOP_UNCONFIRMED")
    policy = OperatorPolicy(trusted_proxy="127.0.0.1", origin="https://operator.test",
                            subjects={"user@example.test"}, scopes={"reconcile_effect", "reconcile_stop"})
    ctx = AppContext(sim, registry, lambda _: None, None, None, operator_policy=policy)
    client = TestClient(TestServer(build_app(ctx)))
    await client.start_server()
    try:
        report = await (await client.get("/api/tasks/legacy-task?assess=1")).json()
        assert report["workflow"] == "LEGACY"
        before = registry.ledger._db.total_changes
        response = await client.post("/api/operator/authorizations", json={
            "scope": "reconcile_stop", "task_id": "legacy-task", "operation_id": "legacy-op",
            "attempt_id": record.attempt_id, "state_digest": report["recovery_review"]["state_digest"],
        }, headers=HEADERS)
        assert response.status == 409 and (await response.json())["error"] == "review_context_unavailable"
        assert registry.ledger._db.total_changes == before
    finally:
        await client.close()
        registry.close()
        sim.close()


@pytest.mark.parametrize("point", ["issued", "note_stop_request", "record_stop", "finished", "controller_rejected"])
async def test_stop_persistence_and_controller_faults_fail_closed(tmp_path, monkeypatch, point):
    client, ctx, result, release, _, _ = await _make_env(tmp_path)
    try:
        if point == "issued":
            original = ctx.registry.ledger._event

            def fail_issue(op, attempt, kind, body):
                if kind == "operator_authorization_issued":
                    raise sqlite3.OperationalError("issue failed")
                return original(op, attempt, kind, body)

            monkeypatch.setattr(ctx.registry.ledger, "_event", fail_issue)
            response = await client.post("/api/operator/authorizations", json=await _payload(client, result), headers=HEADERS)
            assert response.status == 503 and not ctx.operator._pending
            return

        grant = await _issue(client, result)
        challenge = grant["challenge_id"]
        if point == "note_stop_request":
            monkeypatch.setattr(ctx.registry.ledger, "note_stop_request", lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError("disk full")))
            response = await client.post("/api/operator/confirmations", json={"challenge_id": challenge, "confirmed": True}, headers=HEADERS)
            assert response.status == 503 and not ctx.sim._stop_fence
            assert result.operation_id in ctx.registry.ledger.quarantined()
            assert _events(ctx.registry, result.operation_id)[-1][1]["status"] == "failed"
        elif point == "record_stop":
            original_record = ctx.registry.ledger.record_stop
            monkeypatch.setattr(ctx.registry.ledger, "record_stop", lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError("disk full")))
            response = await client.post("/api/operator/confirmations", json={"challenge_id": challenge, "confirmed": True}, headers=HEADERS)
            assert response.status == 503 and ctx.sim._stop_fence is not None
            assert result.operation_id in ctx.registry.ledger.quarantined()
            assert ctx.registry.get_operation(result.operation_id).stop_state == "STOP_UNCONFIRMED"
            # 磁盘恢复后，旧凭据不可重用，但同一操作可通过新审查与新凭据完成对账。
            # After disk recovery, the old challenge stays dead while a fresh review + challenge completes reconciliation.
            assert (await client.post("/api/operator/confirmations", json={"challenge_id": challenge, "confirmed": True}, headers=HEADERS)).status == 409
            monkeypatch.setattr(ctx.registry.ledger, "record_stop", original_record)
            retry_grant = await _issue(client, result)
            retry = await client.post("/api/operator/confirmations",
                                      json={"challenge_id": retry_grant["challenge_id"], "confirmed": True}, headers=HEADERS)
            assert retry.status == 200 and (await retry.json())["stop_state"] == "MOTION_STOPPED_OBSERVED"
            assert not ctx.sim._stop_fence and ctx.registry.ledger.quarantined() == {}
        elif point == "finished":
            original = ctx.registry.ledger._event

            def fail_finish(op, attempt, kind, body):
                if kind == "operator_authorization_finished":
                    raise sqlite3.OperationalError("finish failed")
                return original(op, attempt, kind, body)

            monkeypatch.setattr(ctx.registry.ledger, "_event", fail_finish)
            response = await client.post("/api/operator/confirmations", json={"challenge_id": challenge, "confirmed": True}, headers=HEADERS)
            assert response.status == 503 and not ctx.operator._pending
            assert ctx.registry.get_operation(result.operation_id).stop_state == "MOTION_STOPPED_OBSERVED"
            assert (await client.post("/api/operator/confirmations", json={"challenge_id": challenge, "confirmed": True}, headers=HEADERS)).status == 409
        else:
            monkeypatch.setattr(ctx.sim.stop_controller, "prepare", lambda req: (_ for _ in ()).throw(StopRejected("motion_not_stopped")))
            response = await client.post("/api/operator/confirmations", json={"challenge_id": challenge, "confirmed": True}, headers=HEADERS)
            assert response.status == 409 and (await response.json())["error"] == "motion_not_stopped"
            assert result.operation_id in ctx.registry.ledger.quarantined()
            assert _events(ctx.registry, result.operation_id)[-1][1]["error"] == "StopRejected"
    finally:
        ctx.sim._stop_fence = None
        await _close_env(client, ctx, release)


async def test_scene_preview_fails_closed_while_stop_reconciliation_is_inflight(tmp_path, monkeypatch):
    client, ctx, result, release, _, _ = await _make_env(tmp_path)
    entered, unblock = threading.Event(), threading.Event()
    original = ctx.sim.stop_controller.prepare

    def slow_prepare(req):
        entered.set()
        assert unblock.wait(5)
        return original(req)

    monkeypatch.setattr(ctx.sim.stop_controller, "prepare", slow_prepare)
    try:
        grant = await _issue(client, result)
        pending = asyncio.create_task(client.post(
            "/api/operator/confirmations", json={"challenge_id": grant["challenge_id"], "confirmed": True}, headers=HEADERS,
        ))
        assert await asyncio.to_thread(entered.wait, 2)
        scene = await client.get("/api/scene.jpg")
        assert scene.status == 503 and "stop_reconciliation_in_progress" in await scene.text()
        unblock.set()
        confirm = await asyncio.wait_for(pending, 5)
        assert confirm.status == 200 and (await confirm.json())["stop_state"] == "MOTION_STOPPED_OBSERVED"
    finally:
        unblock.set()
        await _close_env(client, ctx, release)


async def test_websocket_disconnect_quarantine_recovers_via_authenticated_stop_only(tmp_path, monkeypatch):
    from embodied.cognition.engine import PlannerEngine
    from embodied.cognition.offline import SIM_PLAN_RULES, ScriptedPlanProvider
    from embodied.providers.audio import MockASRProvider, MockStreamingTTSProvider
    from embodied.skills.scripted import motions

    sim = TabletopSim(seed=0)
    registry = SkillRegistry(ledger=OperationLedger(tmp_path / "ws.sqlite3"), robot_id=sim.spec().embodiment_id)
    register_sim_skills(registry, sim)
    entered, unblock = threading.Event(), threading.Event()
    original = motions.move_to_q

    def delayed(*args, **kwargs):
        entered.set()
        assert unblock.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(motions, "move_to_q", delayed)
    policy = OperatorPolicy(trusted_proxy="127.0.0.1", origin="https://operator.test",
                            subjects={"user@example.test"}, scopes={"reconcile_effect", "reconcile_stop"})
    ctx = AppContext(sim, registry, lambda confirm: PlannerEngine(
        ScriptedPlanProvider(SIM_PLAN_RULES), registry, confirm=confirm,
    ), MockASRProvider(), MockStreamingTTSProvider(), operator_policy=policy)
    client = TestClient(TestServer(build_app(ctx)))
    await client.start_server()
    try:
        # WebSocket 在运动中途断开会触发撤销、隔离并标记任务 INTERRUPTED。
        # Disconnecting the WebSocket mid-motion revokes the call, quarantines resources and marks the task INTERRUPTED.
        async with client.ws_connect("/ws/session") as ws:
            assert (await ws.receive_json())["type"] == "hello"
            await ws.send_json({"type": "text", "text": "回零"})
            assert (await ws.receive_json())["type"] == "turn_start"
            assert await asyncio.to_thread(entered.wait, 2)
        unblock.set()
        for _ in range(200):
            tasks = await (await client.get("/api/tasks")).json()
            if tasks and tasks[0]["workflow"] == "INTERRUPTED" and all(p.drained for p in registry._stop_contexts.values()):
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("interrupted websocket task did not settle")
        task_id = tasks[0]["task_id"]
        report = await (await client.get(f"/api/tasks/{task_id}?assess=1")).json()
        step = report["steps"][0]
        assert step["call_state"] == "OUTCOME_UNKNOWN" and step["stop_state"] == "STOP_UNCONFIRMED"
        assert report["recommendation"] == "confirm_stop_before_effect_review"

        monkeypatch.setattr(motions, "move_to_q", original)
        # 隔离未解除前，新的 WebSocket 动作指令会被资源仲裁拒绝。
        # New WebSocket motion commands are rejected by resource arbitration while quarantine holds.
        async with client.ws_connect("/ws/session") as ws:
            assert (await ws.receive_json())["type"] == "hello"
            await ws.send_json({"type": "text", "text": "回零"})
            blocked_steps = []
            while True:
                event = await asyncio.wait_for(ws.receive_json(), 10)
                if event["type"] == "step":
                    blocked_steps.append(event)
                if event["type"] == "reply":
                    break
            assert len(blocked_steps) == 1 and blocked_steps[0]["status"] == "failed"
            assert "resource_unavailable" in blocked_steps[0]["detail"]

        # 期间发生了新任务写入，旧审查摘要必须失效；需重新审查后才能签发停止授权。
        # The rejected WS task wrote to the ledger, so the older state_digest must be rejected until re-reviewed.
        stale = await client.post("/api/operator/authorizations", json={
            "scope": "reconcile_stop", "task_id": task_id, "operation_id": step["operation_id"],
            "attempt_id": step["attempt_id"], "state_digest": report["recovery_review"]["state_digest"],
        }, headers=HEADERS)
        assert stale.status == 409 and (await stale.json())["error"] == "review_snapshot_changed"

        # 通过受控 HTTP 重新审查并授权停止解除隔离后，新显式 WebSocket 指令可正常完成。
        # After re-reviewing and authenticating HTTP stop reconciliation to release quarantine, a new explicit WS command succeeds.
        fresh_report = await (await client.get(f"/api/tasks/{task_id}?assess=1")).json()
        grant_res = await client.post("/api/operator/authorizations", json={
            "scope": "reconcile_stop", "task_id": task_id, "operation_id": step["operation_id"],
            "attempt_id": step["attempt_id"], "state_digest": fresh_report["recovery_review"]["state_digest"],
        }, headers=HEADERS)
        grant = await grant_res.json()
        assert grant_res.status == 200, grant
        confirm = await client.post("/api/operator/confirmations",
                                    json={"challenge_id": grant["challenge_id"], "confirmed": True}, headers=HEADERS)
        body = await confirm.json()
        assert confirm.status == 200 and body["stop_state"] == "MOTION_STOPPED_OBSERVED"
        assert body["invocation_state"] == "OUTCOME_UNKNOWN" and not body["task_execution_authorized"]

        async with client.ws_connect("/ws/session") as ws:
            assert (await ws.receive_json())["type"] == "hello"
            await ws.send_json({"type": "text", "text": "回零"})
            ok_steps = []
            while True:
                event = await asyncio.wait_for(ws.receive_json(), 10)
                if event["type"] == "step":
                    ok_steps.append(event)
                if event["type"] == "reply":
                    break
            assert len(ok_steps) == 1 and ok_steps[0]["status"] == "ok"
    finally:
        unblock.set()
        await client.close()
        registry.close()
        sim.close()
