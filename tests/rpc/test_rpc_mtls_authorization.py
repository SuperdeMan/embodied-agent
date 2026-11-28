"""Real BoringSSL mTLS transport auth, role isolation, and audited RPC maintenance.

真实 BoringSSL mTLS 传输认证、角色隔离与可审计 RPC 维护测试。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import subprocess
import threading
from pathlib import Path

import pytest

grpc = pytest.importorskip("grpc")
pytest.importorskip("mujoco")

from embodied.runtime.rpcgen import GEN_DIR  # noqa: E402

if not (GEN_DIR / "embodiedrpc").is_dir():
    pytest.skip("proto stubs not generated", allow_module_level=True)

OPENSSL = shutil.which("openssl")
if OPENSSL is None:
    pytest.skip("openssl binary unavailable for mTLS fixtures", allow_module_level=True)

from embodied.contracts.operation import OperationIdentity  # noqa: E402
from embodied.contracts.rpc_auth import RpcAccessPolicy, RpcTlsPaths  # noqa: E402
from embodied.control import service as control_service  # noqa: E402
from embodied.control.drivers.mujoco_sim import TabletopSim  # noqa: E402
from embodied.control.simassets import find_menagerie_model  # noqa: E402
from embodied.runtime import liveness  # noqa: E402
from embodied.runtime.operations import OperationLedger  # noqa: E402
from embodied.runtime.rpc_auth import (  # noqa: E402
    build_client_channel,
    cert_fingerprint_sha256,
    load_client_credentials,
)
from embodied.runtime.rpcgen import import_common, import_control, import_safety  # noqa: E402
from embodied.safety import guardian as guardian_mod  # noqa: E402
from embodied.safety.guard import GuardLimits  # noqa: E402
from embodied.skills.manifest import SkillManifest  # noqa: E402
from embodied.skills.registry import SkillRegistry, SkillResult  # noqa: E402
from embodied.skills.remote import RemoteSkillRegistry  # noqa: E402
from embodied.skills.scripted import motions  # noqa: E402
from embodied.skills.scripted.manip import register_sim_skills  # noqa: E402

try:
    find_menagerie_model()
except FileNotFoundError:
    pytest.skip("menagerie assets not fetched", allow_module_level=True)


def _issue_ca(directory: Path, name: str) -> tuple[Path, Path]:
    key = directory / f"{name}.key"
    cert = directory / f"{name}.crt"
    subprocess.run([
        OPENSSL, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
        "-nodes", "-keyout", str(key), "-out", str(cert), "-days", "1", "-subj", f"/CN={name}",
    ], check=True, capture_output=True)
    return cert, key


def _issue_leaf(directory: Path, name: str, ca_cert: Path, ca_key: Path, san: str) -> tuple[Path, Path]:
    key = directory / f"{name}.key"
    csr = directory / f"{name}.csr"
    cert = directory / f"{name}.crt"
    ext = directory / f"{name}.ext"
    ext.write_text(f"subjectAltName={san}\nbasicConstraints=CA:FALSE\n", encoding="utf-8")
    subprocess.run([
        OPENSSL, "req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
        "-nodes", "-keyout", str(key), "-out", str(csr), "-subj", f"/CN={name}",
    ], check=True, capture_output=True)
    subprocess.run([
        OPENSSL, "x509", "-req", "-in", str(csr), "-CA", str(ca_cert), "-CAkey", str(ca_key),
        "-CAcreateserial", "-out", str(cert), "-days", "1", "-extfile", str(ext),
    ], check=True, capture_output=True)
    return cert, key


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    root = tmp_path_factory.mktemp("rpc_mtls_pki")
    ca_cert, ca_key = _issue_ca(root, "embodied-ca")
    rogue_ca_cert, rogue_ca_key = _issue_ca(root, "rogue-ca")
    specs = {
        "control": "DNS:control.embodied.test,DNS:localhost,IP:127.0.0.1",
        "guardian": "DNS:guardian.embodied.test,DNS:localhost,IP:127.0.0.1,URI:spiffe://embodied.test/role/guardian",
        "agent": "URI:spiffe://embodied.test/role/agent",
        "op_effect": "URI:spiffe://embodied.test/role/op-effect",
        "op_stop": "URI:spiffe://embodied.test/role/op-stop",
        "op_admin": "URI:spiffe://embodied.test/role/op-admin",
        "unlisted": "URI:spiffe://embodied.test/role/unlisted",
        "wrong_spiffe": "URI:spiffe://embodied.test/role/wrong",
    }
    certs: dict[str, RpcTlsPaths] = {}
    fps: dict[str, str] = {}
    for name, san in specs.items():
        cert, key = _issue_leaf(root, name, ca_cert, ca_key, san)
        certs[name] = RpcTlsPaths(ca_cert=str(ca_cert), cert=str(cert), key=str(key))
        fps[name] = cert_fingerprint_sha256(cert.read_bytes())
    rogue_cert, rogue_key = _issue_leaf(root, "rogue_client", rogue_ca_cert, rogue_ca_key, "URI:spiffe://embodied.test/role/agent")
    certs["rogue"] = RpcTlsPaths(ca_cert=str(ca_cert), cert=str(rogue_cert), key=str(rogue_key))
    certs["wrong_ca_server"] = RpcTlsPaths(ca_cert=str(rogue_ca_cert), cert=certs["control"].cert, key=certs["control"].key)

    policy = RpcAccessPolicy.model_validate({
        "ca_cert_sha256": cert_fingerprint_sha256(ca_cert.read_bytes()),
        "peers": [
            {"name": "agent-core", "role": "agent", "cert_sha256": fps["agent"],
             "spiffe_id": "spiffe://embodied.test/role/agent"},
            {"name": "safety-guardian", "role": "guardian", "cert_sha256": fps["guardian"],
             "spiffe_id": "spiffe://embodied.test/role/guardian"},
            {"name": "op-effect", "role": "operator", "cert_sha256": fps["op_effect"],
             "scopes": ["read_only", "reconcile_effect"]},
            {"name": "op-stop", "role": "operator", "cert_sha256": fps["op_stop"],
             "scopes": ["read_only", "reconcile_stop", "halt"]},
            {"name": "op-admin", "role": "operator", "cert_sha256": fps["op_admin"],
             "scopes": ["read_only", "reset_guard"]},
            {"name": "wrong-spiffe-peer", "role": "operator", "cert_sha256": fps["wrong_spiffe"],
             "spiffe_id": "spiffe://embodied.test/role/expected", "scopes": ["read_only"]},
        ],
    })
    return {"certs": certs, "fps": fps, "policy": policy}


async def test_scoped_mtls_single_step_retry_rejects_other_roles_and_replay(pki, retry_scenario):
    c = retry_scenario
    await c.reconcile()
    pb, rpc = import_control()
    existing = pki["policy"]
    peers = tuple(peer.model_copy(update={"scopes": peer.scopes | {"retry_step"}})
                  if peer.name == "op-stop" else peer for peer in existing.peers)
    policy = existing.model_copy(update={"peers": peers})
    state = control_service.ControlState(c.registry, sim=c.sim)
    server, port = await control_service.serve(state, port=0, tls=pki["certs"]["control"], policy=policy)
    address = f"127.0.0.1:{port}"
    try:
        for peer in ("agent", "guardian", "op_effect"):
            async with build_client_channel(address, pki["certs"][peer]) as channel:
                with pytest.raises(grpc.aio.AioRpcError) as denied:
                    await rpc.ControlServiceStub(channel).PrepareStepRetry(pb.StepRetryRequest(), timeout=3)
                assert denied.value.code() == grpc.StatusCode.PERMISSION_DENIED
        async with build_client_channel(address, pki["certs"]["op_stop"]) as channel:
            stub = rpc.ControlServiceStub(channel)
            review = await stub.ReconcileTask(pb.TaskRequest(task_id=c.identity.task_id), timeout=5)
            state_digest = json.loads(review.report_json)["recovery_review"]["state_digest"]
            prepared = await stub.PrepareStepRetry(pb.StepRetryRequest(task_id=c.identity.task_id,
                operation_id=c.identity.operation_id, attempt_id=c.first.attempt_id, state_digest=state_digest), timeout=5)
            assert not prepared.error, prepared.error
            confirmation = pb.StepRetryConfirmation(challenge_id=json.loads(prepared.report_json)["challenge_id"], confirmed=True)
            executed = await stub.ConfirmStepRetry(confirmation, timeout=30)
            assert not executed.error, executed.error
            assert json.loads(executed.report_json)["effect_verdict"] == "sat"
            assert (await stub.ConfirmStepRetry(confirmation, timeout=3)).error == "operator_challenge_unavailable"
            assert len(c.calls) == 2
    finally:
        await server.stop(0)


async def test_non_loopback_and_misconfigured_tls_fail_closed(pki):
    registry = SkillRegistry()
    cstate = control_service.ControlState(registry)
    gstate = guardian_mod.GuardianState("127.0.0.1:8391", GuardLimits(joint_lower=(), joint_upper=()))
    try:
        with pytest.raises(ValueError, match="non_loopback_rpc_requires_mtls_policy"):
            await control_service.serve(cstate, port=0, host="0.0.0.0")
        with pytest.raises(ValueError, match="non_loopback_rpc_requires_mtls_policy"):
            await guardian_mod.serve(gstate, port=0, host="0.0.0.0")
        with pytest.raises(ValueError, match="rpc_tls_and_policy_must_be_paired"):
            await control_service.serve(cstate, port=0, tls=pki["certs"]["control"])
        with pytest.raises(ValueError, match="rpc_tls_and_policy_must_be_paired"):
            await control_service.serve(cstate, port=0, policy=pki["policy"])
        with pytest.raises(ValueError, match="rpc_ca_cert_fingerprint_mismatch"):
            await control_service.serve(cstate, port=0, tls=pki["certs"]["wrong_ca_server"], policy=pki["policy"])
        with pytest.raises(ValueError, match="non_loopback_rpc_requires_mtls"):
            build_client_channel("10.0.0.5:8391")
        with pytest.raises(ValueError, match="non_loopback_rpc_requires_mtls"):
            await liveness.agent_heartbeat_loop("10.0.0.5:8392")
        with pytest.raises(ValueError, match="non_loopback_rpc_requires_mtls"):
            guardian_mod._open_control_channel(guardian_mod.GuardianState("10.0.0.5:8391"))
    finally:
        registry.close()


async def test_unauthenticated_plaintext_rogue_unlisted_and_wrong_spiffe_rejected(pki):
    pb2, pb2_grpc = import_control()
    registry = SkillRegistry()
    state = control_service.ControlState(registry)
    server, port = await control_service.serve(state, port=0, tls=pki["certs"]["control"], policy=pki["policy"])
    target = f"127.0.0.1:{port}"
    try:
        # 明文连接被 mTLS 服务端拒绝。 / Plaintext client is rejected by the mTLS server.
        async with grpc.aio.insecure_channel(target) as plain:
            with pytest.raises(grpc.aio.AioRpcError) as exc:
                await pb2_grpc.ControlServiceStub(plain).ListSkills(pb2.ListSkillsRequest(), timeout=3)
            assert exc.value.code() in (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.UNAUTHENTICATED)

        # 非受信 CA 签发的客户端证书在 TLS 握手阶段失败。 / Rogue-CA client cert fails TLS handshake.
        async with build_client_channel(target, pki["certs"]["rogue"]) as rogue:
            with pytest.raises(grpc.aio.AioRpcError) as exc:
                await pb2_grpc.ControlServiceStub(rogue).ListSkills(pb2.ListSkillsRequest(), timeout=3)
            assert exc.value.code() in (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.UNAUTHENTICATED)

        # 合法 CA 签发但未在策略固定的证书被 UNAUTHENTICATED 拒绝。
        # Valid CA-signed cert omitted from pinned policy is rejected as UNAUTHENTICATED.
        async with build_client_channel(target, pki["certs"]["unlisted"]) as unlisted:
            with pytest.raises(grpc.aio.AioRpcError) as exc:
                await pb2_grpc.ControlServiceStub(unlisted).ListSkills(pb2.ListSkillsRequest(), timeout=3)
            assert exc.value.code() == grpc.StatusCode.UNAUTHENTICATED and "rpc_peer_not_allowed" in exc.value.details()

        # 指纹匹配但 SPIFFE URI SAN 不匹配的证书被 UNAUTHENTICATED 拒绝。
        # Pinned cert with mismatched SPIFFE URI SAN is rejected as UNAUTHENTICATED.
        async with build_client_channel(target, pki["certs"]["wrong_spiffe"]) as wrong:
            with pytest.raises(grpc.aio.AioRpcError) as exc:
                await pb2_grpc.ControlServiceStub(wrong).ListSkills(pb2.ListSkillsRequest(), timeout=3)
            assert exc.value.code() == grpc.StatusCode.UNAUTHENTICATED and "rpc_spiffe_id_mismatch" in exc.value.details()
    finally:
        await server.stop(None)
        registry.close()


async def test_role_isolation_and_spoofed_metadata_fail_closed_without_side_effects(pki):
    pb2, pb2_grpc = import_control()
    s_pb2, s_pb2_grpc = import_safety()
    common = import_common()
    registry = SkillRegistry()

    async def echo():
        return SkillResult(ok=True, detail="ok")

    registry.register(SkillManifest(name="skill.test.echo", description="echo"), echo)
    cstate = control_service.ControlState(registry, require_supervisor=False)
    cserver, cport = await control_service.serve(cstate, port=0, tls=pki["certs"]["control"], policy=pki["policy"])
    gstate = guardian_mod.GuardianState(
        f"127.0.0.1:{cport}", GuardLimits(joint_lower=(), joint_upper=()),
        control_credentials=load_client_credentials(pki["certs"]["guardian"]),
    )
    gserver, gport = await guardian_mod.serve(gstate, port=0, tls=pki["certs"]["guardian"], policy=pki["policy"])
    spoof_md = (("x-operator", "admin"), ("role", "operator"), ("tailscale-user-login", "admin@example.test"))

    async def assert_denied(coro):
        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await coro
        assert exc.value.code() == grpc.StatusCode.PERMISSION_DENIED

    try:
        # agent 不能伪造 SupervisorLink、急停、复位或对账，即使携带伪造 metadata。
        # Agent cannot call SupervisorLink, Halt, Reset, or reconciliation even with spoofed metadata.
        async with build_client_channel(f"127.0.0.1:{cport}", pki["certs"]["agent"]) as ch:
            stub = pb2_grpc.ControlServiceStub(ch)
            await assert_denied(stub.Halt(pb2.HaltRequest(reason="spoof"), metadata=spoof_md, timeout=3))
            await assert_denied(stub.Reset(pb2.ResetRequest(reason="spoof"), metadata=spoof_md, timeout=3))
            await assert_denied(stub.ReconcileStop(pb2.StopReconciliationRequest(
                operation_id="op", attempt_id="att", confirmed=True), metadata=spoof_md, timeout=3))
            await assert_denied(stub.ReconcileEffect(pb2.EffectRequest(
                operation_id="op", attempt_id="att"), metadata=spoof_md, timeout=3))

            async def one_ping():
                yield common.Timestamp(unix_ms=1)

            with pytest.raises(grpc.aio.AioRpcError) as exc:
                async for _ in stub.SupervisorLink(one_ping(), metadata=spoof_md, timeout=3):
                    pass
            assert exc.value.code() == grpc.StatusCode.PERMISSION_DENIED
            # 未授权 SupervisorLink 绝不能篡改连接状态或触发断连停机。
            # Unauthorized SupervisorLink must never mutate supervisor_connected or trigger halt.
            assert not cstate.supervisor_connected and not cstate.halted

        # guardian 不能调用技能、复位或对账。 / Guardian cannot invoke skills, reset, or reconcile.
        async with build_client_channel(f"127.0.0.1:{cport}", pki["certs"]["guardian"]) as ch:
            stub = pb2_grpc.ControlServiceStub(ch)
            with pytest.raises(grpc.aio.AioRpcError) as exc:
                async for _ in stub.InvokeSkill(pb2.SkillCommand(command_id="c1", skill="skill.test.echo"), timeout=3):
                    pass
            assert exc.value.code() == grpc.StatusCode.PERMISSION_DENIED
            await assert_denied(stub.Reset(pb2.ResetRequest(reason="guardian"), timeout=3))
            await assert_denied(stub.ReconcileStop(pb2.StopReconciliationRequest(
                operation_id="op", attempt_id="att", confirmed=True), timeout=3))
            await assert_denied(stub.ReconcileEffect(pb2.EffectRequest(
                operation_id="op", attempt_id="att"), timeout=3))
            await assert_denied(stub.CancelOperation(pb2.OperationRequest(operation_id="op"), timeout=3))

        # operator 不能调用技能或监督链；未授权作用域严格拒绝。
        # Operators cannot invoke skills or SupervisorLink; ungranted scopes are strictly denied.
        async with build_client_channel(f"127.0.0.1:{cport}", pki["certs"]["op_effect"]) as ch:
            stub = pb2_grpc.ControlServiceStub(ch)
            with pytest.raises(grpc.aio.AioRpcError) as exc:
                async for _ in stub.InvokeSkill(pb2.SkillCommand(command_id="c2", skill="skill.test.echo"), timeout=3):
                    pass
            assert exc.value.code() == grpc.StatusCode.PERMISSION_DENIED
            await assert_denied(stub.ReconcileStop(pb2.StopReconciliationRequest(
                operation_id="op", attempt_id="att", confirmed=True), timeout=3))
            await assert_denied(stub.Halt(pb2.HaltRequest(reason="denied"), timeout=3))
            await assert_denied(stub.Reset(pb2.ResetRequest(reason="denied"), timeout=3))

        # SafetyService：agent 不能触发 EStop，operator 不能伪造 Heartbeat。
        # SafetyService: agent cannot call EStop; operator cannot spoof Heartbeat.
        async with build_client_channel(f"127.0.0.1:{gport}", pki["certs"]["agent"]) as ch:
            await assert_denied(s_pb2_grpc.SafetyServiceStub(ch).EStop(s_pb2.EStopRequest(reason="spoof"), timeout=3))
        async with build_client_channel(f"127.0.0.1:{gport}", pki["certs"]["op_effect"]) as ch:
            sstub = s_pb2_grpc.SafetyServiceStub(ch)

            async def fake_beat():
                yield s_pb2.HeartbeatPing(source="fake")

            with pytest.raises(grpc.aio.AioRpcError) as exc:
                async for _ in sstub.Heartbeat(fake_beat(), timeout=3):
                    pass
            assert exc.value.code() == grpc.StatusCode.PERMISSION_DENIED
            assert not gstate.agent_connected and not gstate.ever_connected
            await assert_denied(sstub.EStop(s_pb2.EStopRequest(reason="no-halt-scope"), timeout=3))

        assert not registry.ledger.has_operations()
    finally:
        await gserver.stop(None)
        await cserver.stop(None)
        registry.close()


async def test_three_process_mtls_liveness_and_estop_chain(pki, monkeypatch):
    monkeypatch.setattr(control_service, "SUPERVISOR_STALE_S", 0.5)
    monkeypatch.setattr(guardian_mod, "AGENT_STALE_S", 0.5)
    monkeypatch.setattr(liveness, "PING_INTERVAL_S", 0.1)
    pb2, pb2_grpc = import_control()
    s_pb2, s_pb2_grpc = import_safety()
    registry = SkillRegistry()

    async def echo():
        return SkillResult(ok=True, detail="echoed")

    registry.register(SkillManifest(name="skill.test.echo", description="echo"), echo)
    cstate = control_service.ControlState(registry, require_supervisor=True)
    cserver, cport = await control_service.serve(cstate, port=0, tls=pki["certs"]["control"], policy=pki["policy"])
    gstate = guardian_mod.GuardianState(
        f"127.0.0.1:{cport}", GuardLimits(joint_lower=(), joint_upper=()),
        control_credentials=load_client_credentials(pki["certs"]["guardian"]),
    )
    gserver, gport = await guardian_mod.serve(gstate, port=0, tls=pki["certs"]["guardian"], policy=pki["policy"])
    link = asyncio.create_task(guardian_mod.supervisor_link_loop(gstate))
    wd = asyncio.create_task(guardian_mod.agent_watchdog_loop(gstate, interval_s=0.1))
    beat = asyncio.create_task(liveness.agent_heartbeat_loop(
        f"127.0.0.1:{gport}", credentials=load_client_credentials(pki["certs"]["agent"]),
    ))
    try:
        for _ in range(50):
            if cstate.supervisor_connected and gstate.agent_connected:
                break
            await asyncio.sleep(0.05)
        assert cstate.supervisor_connected and gstate.agent_connected

        async with build_client_channel(f"127.0.0.1:{cport}", pki["certs"]["agent"]) as agent_ch:
            remote = await RemoteSkillRegistry(agent_ch).connect()
            assert (await remote.invoke("skill.test.echo")).ok

        # 断开 Agent 心跳 -> Guardian 通过 mTLS 触发 Control 急停闩锁。
        # Dropping agent heartbeat causes Guardian to halt Control over mTLS.
        beat.cancel()
        for _ in range(60):
            if cstate.halted:
                break
            await asyncio.sleep(0.05)
        assert cstate.halted and "agent" in cstate.halt_reason

        # 仅拥有 reset_guard 作用域的操作者证书可复位闩锁；无该作用域的操作者被拒绝。
        # Only an operator cert with explicit reset_guard scope can clear halt; others are denied.
        async with build_client_channel(f"127.0.0.1:{cport}", pki["certs"]["op_stop"]) as stop_ch:
            with pytest.raises(grpc.aio.AioRpcError) as exc:
                await pb2_grpc.ControlServiceStub(stop_ch).Reset(pb2.ResetRequest(reason="no-scope"), timeout=3)
            assert exc.value.code() == grpc.StatusCode.PERMISSION_DENIED
            assert cstate.halted

        async with build_client_channel(f"127.0.0.1:{cport}", pki["certs"]["op_admin"]) as admin_ch:
            res = await pb2_grpc.ControlServiceStub(admin_ch).Reset(pb2.ResetRequest(reason="verified-reset"), timeout=3)
            assert res.reset and not cstate.halted
            assert cstate.last_reset_audit["cert_sha256"] == pki["fps"]["op_admin"]

        # 拥有 halt 作用域的操作者通过 SafetyService.EStop 触发 mTLS 急停链。
        # Operator with halt scope triggers mTLS EStop via SafetyService -> Control.Halt.
        async with build_client_channel(f"127.0.0.1:{gport}", pki["certs"]["op_stop"]) as g_op_ch:
            estop = await s_pb2_grpc.SafetyServiceStub(g_op_ch).EStop(s_pb2.EStopRequest(reason="op-estop"), timeout=3)
            assert estop.stopped and cstate.halted and cstate.halt_reason == "op-estop"
    finally:
        beat.cancel()
        wd.cancel()
        link.cancel()
        await gserver.stop(None)
        await cserver.stop(None)
        registry.close()


async def test_mtls_mujoco_stop_effect_audit_and_storage_fault_boundaries(pki, tmp_path, monkeypatch):
    pb2, pb2_grpc = import_control()
    sim = TabletopSim(seed=0)
    registry = SkillRegistry(ledger=OperationLedger(tmp_path / "mtls.sqlite3"), robot_id=sim.spec().embodiment_id)
    register_sim_skills(registry, sim)
    entered, release = threading.Event(), threading.Event()
    original_move = motions.move_to_q

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original_move(*args, **kwargs)

    monkeypatch.setattr(motions, "move_to_q", delayed)
    cstate = control_service.ControlState(registry, sim, world_reader=sim.read_reference_state)
    server, port = await control_service.serve(cstate, port=0, tls=pki["certs"]["control"], policy=pki["policy"])
    target = f"127.0.0.1:{port}"
    try:
        async with (
            build_client_channel(target, pki["certs"]["agent"]) as agent_ch,
            build_client_channel(target, pki["certs"]["op_stop"]) as stop_ch,
            build_client_channel(target, pki["certs"]["op_effect"]) as effect_ch,
            build_client_channel(target, pki["certs"]["op_admin"]) as admin_ch,
        ):
            agent = await RemoteSkillRegistry(agent_ch).connect()
            op_stop = RemoteSkillRegistry(stop_ch)
            op_effect = RemoteSkillRegistry(effect_ch)
            stop_stub = pb2_grpc.ControlServiceStub(stop_ch)
            admin_stub = pb2_grpc.ControlServiceStub(admin_ch)

            # Agent 发起真实 MuJoCo 任务并中途取消，触发隔离。 / Agent starts real MuJoCo task and cancels mid-motion.
            await agent.record_task_plan({"task_id": "t-mtls", "revision": 1, "steps": [{"id": "s1", "skill": "skill.arm.home"}]})
            await agent.record_task_intent("t-mtls", 1, "s1", {})
            oid = (await agent.get_task("t-mtls"))["steps"][0]["operation_id"]
            pending = asyncio.create_task(agent.invoke("skill.arm.home", operation=OperationIdentity(task_id="t-mtls", operation_id=oid)))
            assert await asyncio.to_thread(entered.wait, 3)
            cancelled = await agent.cancel_operation(oid)
            assert not (await pending).ok
            release.set()
            for _ in range(200):
                if registry._stop_contexts[oid].drained:
                    break
                await asyncio.sleep(0.01)

            # ReconcileTask 在 mTLS 下仍只读，不授予执行权限。 / ReconcileTask over mTLS remains read-only with no execution authority.
            review_report = await op_stop.reconcile_task("t-mtls")
            assert not review_report["recovery_review"]["execution_authorized"]
            assert review_report["recovery_review"]["authenticated_subject"] is None
            assert not review_report["automatic_resume_allowed"]

            # 急停闩锁在 mTLS 停止对账后必须保持，且 Reset 不得清除隔离。
            # Guard estop latch persists through mTLS stop reconciliation; Reset never clears quarantine.
            await stop_stub.Halt(pb2.HaltRequest(reason="op-halt"), timeout=3)
            assert sim.guard.halted and oid in registry.ledger.quarantined()

            # 审计写失败必须阻断停止对账（fail-closed），不得进入围栏或推进仿真。
            # Start-audit persistence failure must block stop reconciliation without fencing or stepping physics.
            original_event = registry.ledger._event

            def fail_start(op, att, kind, payload):
                if kind == "rpc_operator_action_started":
                    raise sqlite3.OperationalError("injected audit failure")
                return original_event(op, att, kind, payload)

            monkeypatch.setattr(registry.ledger, "_event", fail_start)
            sim_t = sim.data.time
            audit_fail = await op_stop.reconcile_stop(oid, cancelled.attempt_id, confirmed=True)
            assert audit_fail.reconciliation_error == "rpc_audit_unavailable"
            assert sim.data.time == sim_t and not sim._stop_fence and oid in registry.ledger.quarantined()
            monkeypatch.setattr(registry.ledger, "_event", original_event)

            # 未确认仍被拒绝且不写审计；确认后完成停止对账并按精确顺序落账 4 类事件。
            # Unconfirmed call is denied without audit; confirmed call completes with exact 4-event audit order.
            before_changes = registry.ledger._db.total_changes
            unconfirmed = await stop_stub.ReconcileStop(pb2.StopReconciliationRequest(
                operation_id=oid, attempt_id=cancelled.attempt_id, confirmed=False,
            ), timeout=3)
            assert unconfirmed.reconciliation_error == "confirmation_required"
            assert registry.ledger._db.total_changes == before_changes

            observed = await op_stop.reconcile_stop(oid, cancelled.attempt_id, confirmed=True)
            assert not observed.reconciliation_error
            assert observed.state == "OUTCOME_UNKNOWN" and observed.stop_state == "MOTION_STOPPED_OBSERVED"
            assert sim.guard.halted and registry.ledger.quarantined() == {}

            ev_rows = [
                (r[0], json.loads(r[1]))
                for r in registry.ledger._db.execute(
                    "SELECT event_type, payload_json FROM operation_events WHERE operation_id=? ORDER BY rowid", (oid,),
                )
            ]
            assert [k for k, _ in ev_rows][-4:] == [
                "rpc_operator_action_started", "stop_reconciliation_requested",
                "stop_observed", "rpc_operator_action_finished",
            ]
            assert ev_rows[-1][1]["cert_sha256"] == pki["fps"]["op_stop"] and ev_rows[-1][1]["guard_halted"] is True

            # Reset 仅由 op_admin 执行；随后执行一次抓放并经 op_effect 做已审计效果重查。
            # Only op_admin clears halt; then run a pick and perform an audited ReconcileEffect via op_effect.
            monkeypatch.setattr(motions, "move_to_q", original_move)
            await admin_stub.Reset(pb2.ResetRequest(reason="after-stop"), timeout=3)
            assert not sim.guard.halted
            pick = await agent.invoke("skill.manip.pick")
            assert pick.ok
            eff = await op_effect.reconcile_effect(pick.operation_id, pick.attempt_id)
            assert not eff.reconciliation_error
            eff_events = [
                (r[0], json.loads(r[1]))
                for r in registry.ledger._db.execute(
                    "SELECT event_type, payload_json FROM operation_events WHERE operation_id=? ORDER BY rowid",
                    (pick.operation_id,),
                )
            ]
            assert [k for k, _ in eff_events][-3:] == [
                "rpc_operator_action_started", "effect_observed", "rpc_operator_action_finished",
            ]
            assert eff_events[-1][1]["cert_sha256"] == pki["fps"]["op_effect"]

            # D028 不变式：即使 SQLite 写入失败，Halt 仍立即闩锁驱动 Guard。
            # D028 invariant: even under SQLite failure, Halt still latches the driver Guard immediately.
            monkeypatch.setattr(registry.ledger, "_event", lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError("disk full")))
            halt_reply = await stop_stub.Halt(pb2.HaltRequest(reason="disk-full-halt"), timeout=3)
            assert halt_reply.halted and sim.guard.halted
    finally:
        release.set()
        await server.stop(None)
        registry.close()
        sim.close()


async def test_mtls_effect_audit_failure_finish_audit_failure_and_reset_preserves_quarantine(pki, tmp_path, monkeypatch):
    pb2, pb2_grpc = import_control()
    sim = TabletopSim(seed=0)
    registry = SkillRegistry(ledger=OperationLedger(tmp_path / "faults.sqlite3"), robot_id=sim.spec().embodiment_id)
    register_sim_skills(registry, sim)
    cstate = control_service.ControlState(registry, sim, world_reader=sim.read_reference_state)
    server, port = await control_service.serve(cstate, port=0, tls=pki["certs"]["control"], policy=pki["policy"])
    target = f"127.0.0.1:{port}"
    entered, release = threading.Event(), threading.Event()
    original_move = motions.move_to_q
    try:
        async with (
            build_client_channel(target, pki["certs"]["agent"]) as agent_ch,
            build_client_channel(target, pki["certs"]["op_effect"]) as effect_ch,
            build_client_channel(target, pki["certs"]["op_stop"]) as stop_ch,
            build_client_channel(target, pki["certs"]["op_admin"]) as admin_ch,
        ):
            agent = await RemoteSkillRegistry(agent_ch).connect()
            op_effect = RemoteSkillRegistry(effect_ch)
            op_stop = RemoteSkillRegistry(stop_ch)
            stop_stub = pb2_grpc.ControlServiceStub(stop_ch)
            admin_stub = pb2_grpc.ControlServiceStub(admin_ch)

            # 1. ReconcileEffect 开始审计写库失败 -> 拒绝采样，不产生新 effect_observed。
            # Start-audit DB failure on ReconcileEffect refuses sampling with zero new effect_observed.
            pick = await agent.invoke("skill.manip.pick")
            assert pick.ok
            before_effects = registry.ledger._db.execute(
                "SELECT count(*) FROM operation_events WHERE event_type='effect_observed'",
            ).fetchone()[0]
            orig_event = registry.ledger._event

            def fail_start(op, att, kind, payload):
                if kind == "rpc_operator_action_started":
                    raise sqlite3.OperationalError("start audit failed")
                return orig_event(op, att, kind, payload)

            monkeypatch.setattr(registry.ledger, "_event", fail_start)
            eff_fail = await op_effect.reconcile_effect(pick.operation_id, pick.attempt_id)
            assert eff_fail.reconciliation_error == "rpc_audit_unavailable"
            assert registry.ledger._db.execute(
                "SELECT count(*) FROM operation_events WHERE event_type='effect_observed'",
            ).fetchone()[0] == before_effects
            monkeypatch.setattr(registry.ledger, "_event", orig_event)

            # 2. Reset 不清除未知操作隔离；ReconcileStop 结束审计失败时报错但保留已提交 stop_observed；重复调用幂等不重跑。
            # Reset never clears quarantine; finish-audit failure reports error while keeping stop_observed; repeat is read-only.
            def delayed(*args, **kwargs):
                entered.set()
                assert release.wait(5)
                return original_move(*args, **kwargs)

            monkeypatch.setattr(motions, "move_to_q", delayed)
            oid = "op-quarantined"
            pending = asyncio.create_task(agent.invoke("skill.arm.home", operation=OperationIdentity(task_id="t-q", operation_id=oid)))
            assert await asyncio.to_thread(entered.wait, 3)
            cancelled = await agent.cancel_operation(oid)
            assert not (await pending).ok
            release.set()
            for _ in range(200):
                if registry._stop_contexts[oid].drained:
                    break
                await asyncio.sleep(0.01)

            await stop_stub.Halt(pb2.HaltRequest(reason="estop"), timeout=3)
            assert sim.guard.halted and oid in registry.ledger.quarantined()
            await admin_stub.Reset(pb2.ResetRequest(reason="reset-only"), timeout=3)
            assert not sim.guard.halted and oid in registry.ledger.quarantined()
            blocked = await agent.invoke("skill.arm.home")
            assert not blocked.ok and "resource_unavailable" in blocked.detail

            def fail_finish(op, att, kind, payload):
                if kind == "rpc_operator_action_finished":
                    raise sqlite3.OperationalError("finish audit failed")
                return orig_event(op, att, kind, payload)

            monkeypatch.setattr(registry.ledger, "_event", fail_finish)
            fin_err = await op_stop.reconcile_stop(oid, cancelled.attempt_id, confirmed=True)
            assert fin_err.reconciliation_error == "rpc_audit_unavailable"
            assert registry.get_operation(oid).stop_state == "MOTION_STOPPED_OBSERVED"
            assert registry.ledger.quarantined() == {}
            monkeypatch.setattr(registry.ledger, "_event", orig_event)

            # 重复 mTLS ReconcileStop 返回历史证据，不推进仿真、不写新审计。
            # Repeated mTLS ReconcileStop returns historical evidence read-only without stepping physics or auditing again.
            sim_t = sim.data.time
            changes = registry.ledger._db.total_changes
            repeat = await op_stop.reconcile_stop(oid, cancelled.attempt_id, confirmed=True)
            assert not repeat.reconciliation_error and repeat.stop_state == "MOTION_STOPPED_OBSERVED"
            assert sim.data.time == sim_t and registry.ledger._db.total_changes == changes
    finally:
        release.set()
        await server.stop(None)
        registry.close()
        sim.close()


def test_cli_refuses_non_loopback_plaintext_and_incomplete_tls(pki):
    from embodied.cli import main

    with pytest.raises(ValueError, match="non_loopback_rpc_requires_mtls_policy"):
        main(["serve-control", "--host", "0.0.0.0", "--mock"])
    with pytest.raises(ValueError, match="non_loopback_rpc_requires_mtls"):
        main(["operation", "--remote", "10.0.0.5:8391", "--id", "op1"])
    with pytest.raises(ValueError, match="incomplete_mtls_paths"):
        main(["operation", "--remote", "127.0.0.1:8391", "--id", "op1", "--tls-ca", pki["certs"]["agent"].ca_cert])
