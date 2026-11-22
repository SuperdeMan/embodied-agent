"""Contract and process-lifecycle tests for ControlProcess (D034).

隔离物理控制子进程监督器（ControlProcess）的契约、故障回收、急停强杀与生命周期测试（D034）。
"""

from __future__ import annotations

import io
import os
import struct
import sys
import sysconfig
import threading
import time
from pathlib import Path

import pytest

from embodied.contracts.control_process import (
    ALLOWED_CONTROL_OPERATIONS,
    ControlWorkerEnvelope,
    encode_control_frame,
    read_control_frame,
)
from embodied.runtime.control_process import ControlProcess, PhysicalControlUnavailable
from embodied.runtime.invocation import CURRENT_INVOCATION, InvocationPermit

FIXTURE = Path(__file__).parent / "control_worker_fixture.py"


def _fixture_command(*extra: str) -> list[str]:
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
        *extra,
    ]


def test_control_envelope_strict_framing_and_whitelist():
    env = ControlWorkerEnvelope(boot_id="b1", request_id="1", kind="call", payload_json='{"a":1}')
    framed = encode_control_frame(env)
    decoded = read_control_frame(io.BytesIO(framed))
    assert decoded == env

    # 拒绝超长帧与重复字段或非有限数。 / Reject oversize frames, duplicate keys, and non-finite numbers.
    with pytest.raises(ValueError, match="length"):
        read_control_frame(io.BytesIO(struct.pack("!I", 20 * 1024 * 1024)))
    bad_dup = b'{"api_version":"embodied.dev/v1alpha1","api_version":"embodied.dev/v1alpha1","boot_id":"b","request_id":"1","kind":"ok","payload_json":"{}"}'
    with pytest.raises(ValueError, match="duplicate"):
        read_control_frame(io.BytesIO(struct.pack("!I", len(bad_dup)) + bad_dup))
    assert "step" in ALLOWED_CONTROL_OPERATIONS and "eval" not in ALLOWED_CONTROL_OPERATIONS


def test_control_process_budget_and_secret_filtering(monkeypatch):
    with pytest.raises(ValueError, match="finite and positive"):
        ControlProcess({}, timeout_s=0)
    with pytest.raises(ValueError, match="finite and positive"):
        ControlProcess({}, cancel_grace_s=float("nan"))

    monkeypatch.setenv("CONTROL_TEST_SECRET", "top-secret-token")
    worker = ControlProcess({}, timeout_s=2.0, startup_timeout_s=10.0, _command=_fixture_command("--mode", "stub"))
    try:
        meta = worker.start()
        assert meta["pid"] != os.getpid()
        assert meta["has_secret"] is False
        assert worker.state == "READY"
        with pytest.raises(ValueError, match="not allowed"):
            worker.call("arbitrary_exec", {})
    finally:
        worker.close()
    assert worker.state == "CLOSED" and worker.reaped
    assert worker.shutdown_acknowledged and worker.last_exitcode == 0
    # 终态 CLOSED 不可被重新激活。 / Terminal CLOSED cannot be revived.
    with pytest.raises(PhysicalControlUnavailable, match="closed"):
        worker.start()
    with pytest.raises(PhysicalControlUnavailable, match="closed"):
        worker.restart()


@pytest.mark.parametrize("fault", ["hang", "gil_hang", "crash", "abort", "wrong_boot", "wrong_request", "oversize", "error"])
def test_control_process_reaps_every_fault_without_replay(tmp_path, fault):
    trace = tmp_path / f"{fault}.txt"
    faults_seen: list[str] = []
    worker = ControlProcess(
        {"trace": str(trace)},
        timeout_s=0.15,
        startup_timeout_s=10.0,
        on_fault=faults_seen.append,
        _command=_fixture_command("--mode", "stub"),
    )
    try:
        worker.start()
        old_boot, old_pid = worker.boot_id, worker.last_pid
        started = time.monotonic()
        with pytest.raises(PhysicalControlUnavailable):
            worker.call("step", {"fault": fault})
        elapsed = time.monotonic() - started
        assert elapsed < 3.0
        assert worker.state == "FAILED" and worker.reaped
        assert len(faults_seen) == 1
        # 不静默重发也不自动重启。 / Never replay the failed call or auto-restart.
        assert trace.read_text(encoding="utf-8").splitlines() == ["step:"]
        with pytest.raises(PhysicalControlUnavailable, match="explicit_restart"):
            worker.call("read", {})
        assert trace.read_text(encoding="utf-8").splitlines() == ["step:"]
        # 显式重启后换新代次且继续不重放旧请求。 / Explicit restart rotates generation without replaying.
        worker.restart()
        assert worker.state == "READY" and worker.boot_id != old_boot and worker.last_pid != old_pid
        assert trace.read_text(encoding="utf-8").splitlines() == ["step:"]
        reply = worker.call("read", {})
        assert reply["op"] == "read" and reply["pid"] == worker.last_pid
    finally:
        worker.close()
    assert worker.reaped


def test_urgent_estop_preserves_healthy_worker_and_kills_hung_worker(tmp_path):
    # 1. 健康子进程收到 urgent_estop 仅同步闩锁，保持 READY。 / Healthy worker syncs estop and stays READY.
    healthy = ControlProcess(
        {}, timeout_s=2.0, startup_timeout_s=10.0, cancel_grace_s=0.15,
        _command=_fixture_command("--mode", "stub"),
    )
    try:
        healthy.start()
        assert healthy.urgent_estop("guardian_halt") is True
        assert healthy.state == "READY"
    finally:
        healthy.close()

    # 2. 原生持 GIL 挂起子进程收到 urgent_estop 在宽限窗后被跨线程强杀回收。
    # 2. A GIL-hung worker is forcibly killed and reaped cross-thread by urgent_estop after cancel_grace_s.
    faults: list[str] = []
    hung = ControlProcess(
        {}, timeout_s=10.0, startup_timeout_s=10.0, cancel_grace_s=0.12, on_fault=faults.append,
        _command=_fixture_command("--mode", "stub"),
    )
    try:
        hung.start()
        entered = threading.Event()
        errors: list[BaseException] = []

        def run_hung_call():
            entered.set()
            try:
                hung.call("step", {"fault": "gil_hang"})
            except BaseException as exc:
                errors.append(exc)

        t = threading.Thread(target=run_hung_call)
        t.start()
        assert entered.wait(2.0)
        # 确保请求已在途占用单在途锁。 / Ensure the hung call holds the single-flight lock.
        deadline = time.monotonic() + 2.0
        while not hung.busy and time.monotonic() < deadline:
            time.sleep(0.005)
        assert hung.busy

        t0 = time.monotonic()
        soft = hung.urgent_estop("guardian_hang_kill")
        elapsed = time.monotonic() - t0
        t.join(timeout=2.0)
        assert soft is False
        assert not t.is_alive()
        assert elapsed < 2.0
        assert hung.state == "FAILED" and hung.reaped
        assert len(errors) == 1 and isinstance(errors[0], PhysicalControlUnavailable)
        assert len(faults) == 1
    finally:
        hung.close()


def test_cancelled_permit_reaps_hung_worker_within_grace():
    worker = ControlProcess(
        {}, timeout_s=10.0, startup_timeout_s=10.0, cancel_grace_s=0.1,
        _command=_fixture_command("--mode", "stub"),
    )
    try:
        worker.start()
        permit = InvocationPermit(10.0, lambda: True)
        errors: list[BaseException] = []

        def run_call():
            token = CURRENT_INVOCATION.set(permit)
            try:
                worker.call("step", {"fault": "gil_hang"})
            except BaseException as exc:
                errors.append(exc)
            finally:
                CURRENT_INVOCATION.reset(token)

        t = threading.Thread(target=run_call)
        t.start()
        deadline = time.monotonic() + 2.0
        while not worker.busy and time.monotonic() < deadline:
            time.sleep(0.005)
        t0 = time.monotonic()
        permit.revoke()
        t.join(timeout=2.5)
        assert not t.is_alive()
        assert time.monotonic() - t0 < 2.0
        assert worker.state == "FAILED" and worker.reaped
        assert errors and isinstance(errors[0], PhysicalControlUnavailable)
    finally:
        worker.close()
