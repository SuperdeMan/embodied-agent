"""End-to-end mTLS Guardian Halt killing a natively hung isolated physical control worker (D033 + D034).

端到端验证 D033 BoringSSL mTLS 监督链路与 D034 物理控制进程隔离：
物理子进程原生持 GIL 挂起期间，mTLS Guardian Halt 保持响应并在有界时间内强杀回收卡死子进程。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import sysconfig
import time
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
from embodied.control.isolated_sim import IsolatedTabletopSim  # noqa: E402
from embodied.control.simassets import find_menagerie_model  # noqa: E402
from embodied.runtime import liveness  # noqa: E402
from embodied.runtime.operations import OperationLedger  # noqa: E402
from embodied.runtime.rpc_auth import (  # noqa: E402
    build_client_channel,
    cert_fingerprint_sha256,
    load_client_credentials,
)
from embodied.runtime.rpcgen import import_control  # noqa: E402
from embodied.safety import guardian as guardian_mod  # noqa: E402
from embodied.safety.guard import GuardLimits  # noqa: E402
from embodied.skills.registry import SkillRegistry  # noqa: E402
from embodied.skills.remote import RemoteSkillRegistry  # noqa: E402
from embodied.skills.scripted.manip import register_sim_skills  # noqa: E402

try:
    find_menagerie_model()
except FileNotFoundError:
    pytest.skip("menagerie assets not fetched", allow_module_level=True)

FIXTURE = Path(__file__).parents[1] / "runtime" / "control_worker_fixture.py"


def _real_fixture_command(fault_file: Path, trace_file: Path) -> list[str]:
    bootstrap = (
        "import sys,site,runpy; p=sys.argv.pop(1); sys.path.insert(0,p); site.addsitedir(p); "
        "runpy.run_path(sys.argv.pop(1),run_name='__main__')"
    )
    return [
        getattr(sys, "_base_executable", sys.executable),
        "-I",
        "-u",
        "-c",
        bootstrap,
        sysconfig.get_path("purelib"),
        str(FIXTURE),
        "--mode",
        "real",
        "--fault-file",
        str(fault_file),
        "--trace",
        str(trace_file),
    ]


def _make_pki(root: Path) -> tuple[dict[str, RpcTlsPaths], RpcAccessPolicy]:
    ca_key, ca_cert = root / "ca.key", root / "ca.crt"
    subprocess.run([
        OPENSSL, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
        "-nodes", "-keyout", str(ca_key), "-out", str(ca_cert), "-days", "1", "-subj", "/CN=embodied-d034-ca",
    ], check=True, capture_output=True)

    def issue(name: str, san: str) -> RpcTlsPaths:
        key, csr, cert, ext = root / f"{name}.key", root / f"{name}.csr", root / f"{name}.crt", root / f"{name}.ext"
        ext.write_text(f"subjectAltName={san}\nbasicConstraints=CA:FALSE\n", encoding="utf-8")
        subprocess.run([
            OPENSSL, "req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
            "-nodes", "-keyout", str(key), "-out", str(csr), "-subj", f"/CN={name}",
        ], check=True, capture_output=True)
        subprocess.run([
            OPENSSL, "x509", "-req", "-in", str(csr), "-CA", str(ca_cert), "-CAkey", str(ca_key),
            "-CAcreateserial", "-out", str(cert), "-days", "1", "-extfile", str(ext),
        ], check=True, capture_output=True)
        return RpcTlsPaths(ca_cert=str(ca_cert), cert=str(cert), key=str(key))

    tls = {
        "control": issue("control", "DNS:localhost,IP:127.0.0.1"),
        "guardian": issue("guardian", "DNS:localhost,IP:127.0.0.1,URI:spiffe://embodied.local/role/guardian"),
        "agent": issue("agent", "URI:spiffe://embodied.local/role/agent"),
        "ops": issue("ops", "URI:spiffe://embodied.local/role/ops"),
    }
    policy = RpcAccessPolicy(
        ca_cert_sha256=cert_fingerprint_sha256(ca_cert.read_bytes()),
        peers=(
            {
                "name": "agent-core",
                "role": "agent",
                "cert_sha256": cert_fingerprint_sha256(Path(tls["agent"].cert).read_bytes()),
                "spiffe_id": "spiffe://embodied.local/role/agent",
            },
            {
                "name": "safety-guardian",
                "role": "guardian",
                "cert_sha256": cert_fingerprint_sha256(Path(tls["guardian"].cert).read_bytes()),
                "spiffe_id": "spiffe://embodied.local/role/guardian",
            },
            {
                "name": "ops-admin",
                "role": "operator",
                "cert_sha256": cert_fingerprint_sha256(Path(tls["ops"].cert).read_bytes()),
                "scopes": ("read_only", "reconcile_stop", "halt", "reset_guard"),
            },
        ),
    )
    return tls, policy


async def test_mtls_guardian_halt_kills_hung_physical_worker_and_enforces_reset_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(guardian_mod, "AGENT_STALE_S", 0.35)
    monkeypatch.setattr(liveness, "PING_INTERVAL_S", 0.08)

    tls, policy = _make_pki(tmp_path / "pki" if (tmp_path / "pki").mkdir() or True else tmp_path)
    fault_file = tmp_path / "hang.json"
    trace_file = tmp_path / "trace.txt"
    sim = IsolatedTabletopSim(
        seed=0,
        control_timeout_s=10.0,
        cancel_grace_s=0.12,
        _control_command=_real_fixture_command(fault_file, trace_file),
    )
    registry = SkillRegistry(ledger=OperationLedger(tmp_path / "ops.sqlite3"), robot_id=sim.spec().embodiment_id)
    register_sim_skills(registry, sim)
    c_state = control_service.ControlState(
        registry, sim, require_supervisor=True, world_reader=sim.read_reference_state,
    )
    c_server, c_port = await control_service.serve(c_state, port=0, tls=tls["control"], policy=policy)
    c_addr = f"127.0.0.1:{c_port}"
    g_state = guardian_mod.GuardianState(
        c_addr,
        GuardLimits(joint_lower=(), joint_upper=()),
        control_credentials=load_client_credentials(tls["guardian"]),
    )
    g_server, g_port = await guardian_mod.serve(g_state, port=0, tls=tls["guardian"], policy=policy)
    g_addr = f"127.0.0.1:{g_port}"

    bg = [
        asyncio.create_task(control_service.watchdog_loop(c_state, interval_s=0.05)),
        asyncio.create_task(guardian_mod.supervisor_link_loop(g_state)),
        asyncio.create_task(guardian_mod.agent_watchdog_loop(g_state, interval_s=0.05)),
    ]
    agent_beat = asyncio.create_task(
        liveness.agent_heartbeat_loop(g_addr, credentials=load_client_credentials(tls["agent"]))
    )

    pb2, pb2_grpc = import_control()
    agent_ch = build_client_channel(c_addr, tls["agent"])
    ops_ch = build_client_channel(c_addr, tls["ops"])
    try:
        # 等待监督链路与代理心跳就绪。 / Wait for SupervisorLink and agent heartbeats to stabilize.
        deadline = time.monotonic() + 5.0
        while (not c_state.supervision_ok() or not g_state.ever_connected) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert c_state.supervision_ok() and g_state.ever_connected

        agent_rem = await RemoteSkillRegistry(agent_ch).connect()
        ops_rem = RemoteSkillRegistry(ops_ch)
        ops_stub = pb2_grpc.ControlServiceStub(ops_ch)

        # 注入原生持 GIL 挂起并经 mTLS 触发远程技能。 / Inject native GIL hang and invoke skill over mTLS.
        fault_file.write_text(json.dumps({"operation": "step", "after": 1, "fault": "gil_hang", "once": True}), encoding="utf-8")
        op = OperationIdentity(task_id="t-mtls-hang", operation_id="op-mtls-hang")
        invoke_task = asyncio.create_task(agent_rem.invoke("skill.arm.home", operation=op))

        deadline = time.monotonic() + 3.0
        while (not trace_file.exists() or "step:" not in trace_file.read_text(encoding="utf-8")) and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert sim._control_process.busy

        # 模拟 agent 掉线：guardian 经 mTLS 触发 Halt，必须在有界时间内强杀回收卡死的物理子进程。
        # Drop agent heartbeat: guardian triggers mTLS Halt, which must kill & reap the hung physics worker cleanly.
        t0 = time.monotonic()
        agent_beat.cancel()
        with pytest.raises(asyncio.CancelledError):
            await agent_beat

        result = await asyncio.wait_for(invoke_task, timeout=4.0)
        elapsed = time.monotonic() - t0
        assert elapsed < 4.0
        assert not result.ok
        assert c_state.halted and sim.guard.halted
        assert sim.control_process_state == "FAILED" and sim._control_process.reaped

        # 控制服务保持响应：mTLS GetOperation 立即返回 UNKNOWN/STOP_UNCONFIRMED；FAILED 期间 Reset 报 UNAVAILABLE。
        # ControlService stays responsive: mTLS GetOperation reports UNKNOWN/STOP_UNCONFIRMED, Reset returns UNAVAILABLE.
        op_reply = await ops_rem.get_operation(op.operation_id)
        assert op_reply.found and op_reply.state == "OUTCOME_UNKNOWN" and op_reply.stop_state == "STOP_UNCONFIRMED"
        with pytest.raises(grpc.aio.AioRpcError) as exc_info:
            await ops_stub.Reset(pb2.ResetRequest(reason="too_early"))
        assert exc_info.value.code() == grpc.StatusCode.UNAVAILABLE
        assert c_state.halted and sim.guard.halted

        # 显式重启物理控制子进程（保留 Guard 闩锁与隔离），再经 mTLS 完成 ReconcileStop 与 Reset。
        # Explicitly restart the physical worker (preserving Guard latch & quarantine), then ReconcileStop + Reset over mTLS.
        registry.restart_physical_control()
        assert sim.control_process_state == "READY" and sim.guard.halted and c_state.halted

        stop_reply = await ops_rem.reconcile_stop(op.operation_id, op_reply.attempt_id, confirmed=True)
        assert not stop_reply.reconciliation_error
        assert stop_reply.stop_state == "MOTION_STOPPED_OBSERVED"
        assert sim.guard.halted and c_state.halted

        reset_reply = await ops_stub.Reset(pb2.ResetRequest(reason="operator_cleared"))
        assert reset_reply.reset is True and not c_state.halted and not sim.guard.halted
    finally:
        agent_beat.cancel()
        for t in bg:
            t.cancel()
        await asyncio.gather(*bg, return_exceptions=True)
        await agent_ch.close()
        await ops_ch.close()
        await g_server.stop(None)
        await c_server.stop(None)
        await registry.drain_compute()
        registry.close()
        sim.close()
