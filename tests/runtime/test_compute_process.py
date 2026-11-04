"""Real process death, deadlines and explicit generations. 真实进程退出、期限与显式代次测试。"""

import asyncio
import os
import sys
import sysconfig
import threading
import time
from pathlib import Path

import pytest

from embodied.runtime.compute import ComputeProcess, ComputeUnavailable
from embodied.runtime.invocation import CURRENT_INVOCATION, InvocationPermit
from embodied.runtime.observation import ObservationLane, ObservationUnavailable


def fixture_worker(tmp_path, **config):
    script = Path(__file__).with_name("compute_fixture.py")
    bootstrap = "import sys,site,runpy; p=sys.argv.pop(1);sys.path.insert(0,p);site.addsitedir(p);runpy.run_path(sys.argv.pop(1),run_name='__main__')"
    command = [getattr(sys, "_base_executable", sys.executable), "-I", "-u", "-c", bootstrap,
               sysconfig.get_path("purelib"), str(script)]
    return ComputeProcess({"trace": str(tmp_path / "calls.txt"), **config}, timeout_s=0.15, startup_timeout_s=5,
                          _command=command)


def alive(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes as w
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes, api.OpenProcess.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
        api.GetExitCodeProcess.argtypes, api.GetExitCodeProcess.restype = [w.HANDLE, ctypes.POINTER(w.DWORD)], w.BOOL
        api.CloseHandle.argtypes = [w.HANDLE]
        handle = api.OpenProcess(0x00100000 | 0x1000, False, pid)
        if not handle:
            return False
        try:
            code = w.DWORD()
            assert api.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259
        finally:
            api.CloseHandle(handle)
    status = Path(f"/proc/{pid}/stat")
    return status.exists() and status.read_text().split(")", 1)[1].split()[0] != "Z"


@pytest.mark.parametrize("fault", ["hang", "gil_hang", "crash", "abort", "wrong_boot", "wrong_request",
                                    "oversize", "partial", "bad_json"])
def test_fault_reaps_process_and_never_replays_request(tmp_path, fault):
    worker = fixture_worker(tmp_path)
    worker.start()
    old_boot, old_pid = worker.boot_id, worker.last_pid
    started = time.monotonic()
    try:
        with pytest.raises(ComputeUnavailable):
            worker.call(fault, {})
        assert time.monotonic() - started < 3
        assert worker.state == "FAILED" and worker.reaped and not alive(old_pid)
        with pytest.raises(ComputeUnavailable, match="restart"):
            worker.call("echo", {"value": "must not run"})
        assert (tmp_path / "calls.txt").read_text().splitlines() == [fault]
        worker.restart()
        assert worker.boot_id != old_boot
        assert (tmp_path / "calls.txt").read_text().splitlines() == [fault]
        assert worker.call("echo", {"value": "new"})["value"] == "new"
    finally:
        worker.close()
    assert worker.reaped


def test_startup_timeout_and_shutdown_hang_are_bounded(tmp_path):
    worker = fixture_worker(tmp_path, startup_hang=True)
    worker.startup_timeout_s = 0.1
    started = time.monotonic()
    with pytest.raises(ComputeUnavailable):
        worker.start()
    assert worker.reaped and time.monotonic() - started < 3
    worker.close()
    other = fixture_worker(tmp_path, shutdown_hang=True)
    other.start()
    started = time.monotonic()
    other.close()
    assert other.reaped and time.monotonic() - started < 3


def test_owned_descendants_are_stopped_on_normal_close_and_fault(tmp_path):
    for fault in (False, True):
        worker = fixture_worker(tmp_path)
        worker.start()
        child = worker.call("spawn", {})["pid"]
        assert alive(child)
        if fault:
            with pytest.raises(ComputeUnavailable):
                worker.call("crash", {})
        worker.close()
        for _ in range(100):
            if not alive(child):
                break
            time.sleep(0.01)
        assert not alive(child) and worker.reaped


def test_unfinished_io_owner_blocks_replacement_generation(tmp_path, monkeypatch):
    worker = fixture_worker(tmp_path)
    release = threading.Event()
    monkeypatch.setattr(worker, "_pump", lambda: release.wait(4))
    worker.startup_timeout_s = 0.02
    try:
        with pytest.raises(ComputeUnavailable, match="not_reaped"):
            worker.start()
        old_thread = worker._thread
        with pytest.raises(ComputeUnavailable, match="restart_not_ready"):
            worker.restart()
        assert worker._thread is old_thread and worker.state == "FAILED"
    finally:
        release.set()
        worker.close()


def test_parent_exit_stops_its_direct_worker(tmp_path):
    import json
    import subprocess

    marker = tmp_path / "pid.json"
    code = ("import sys,site,runpy; p=sys.argv.pop(1);sys.path.insert(0,p);site.addsitedir(p);"
            "from embodied.runtime.compute import ComputeProcess; import pathlib,json,os;"
            "w=ComputeProcess({'backend':'color'});w.start();"
            "pathlib.Path(sys.argv[1]).write_text(json.dumps({'pid':w.last_pid}));os._exit(0)")
    parent = subprocess.run([getattr(sys, "_base_executable", sys.executable), "-I", "-c", code,
                             sysconfig.get_path("purelib"), str(marker)], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=15)
    assert parent.returncode == 0, parent.stderr
    pid = json.loads(marker.read_text())["pid"]
    for _ in range(100):
        if not alive(pid):
            break
        time.sleep(0.01)
    assert not alive(pid)


def test_one_request_no_queue_and_close_cancels_inflight_work(tmp_path):
    worker = fixture_worker(tmp_path)
    worker.timeout_s = 5
    worker.start()
    errors = []

    def call():
        try:
            worker.call("hang", {})
        except ComputeUnavailable:
            errors.append("stopped")

    thread = threading.Thread(target=call)
    thread.start()
    for _ in range(100):
        if (tmp_path / "calls.txt").exists():
            break
        time.sleep(0.005)
    with pytest.raises(ComputeUnavailable, match="busy"):
        worker.call("echo", {})
    worker.close()
    thread.join(timeout=1)
    assert not thread.is_alive() and errors == ["stopped"] and worker.reaped
    assert (tmp_path / "calls.txt").read_text().splitlines() == ["hang"]


def test_environment_excludes_tokens_and_shutdown_ack_is_separate(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPUTE_TEST_SECRET", "do-not-forward")
    worker = fixture_worker(tmp_path)
    assert worker.start()["has_secret"] is False
    worker.close()
    assert worker.shutdown_acknowledged and worker.last_exitcode == 0 and worker.reaped


def test_closed_client_cannot_be_reactivated_by_a_failed_call(tmp_path):
    worker = fixture_worker(tmp_path)
    worker.start()
    worker.close()
    with pytest.raises(ComputeUnavailable):
        worker.call("echo", {})
    assert worker.state == "CLOSED"
    with pytest.raises(ComputeUnavailable, match="restart_not_ready"):
        worker.restart()
    assert worker.reaped


def test_restart_configuration_is_a_snapshot_not_a_mutable_alias(tmp_path):
    worker = fixture_worker(tmp_path)
    inspected = worker.config
    inspected["startup_hang"] = True
    assert "startup_hang" not in worker.config
    try:
        worker.start()
        assert worker.call("echo", {"value": "unchanged"})["value"] == "unchanged"
    finally:
        worker.close()


async def test_native_gil_hang_does_not_block_parent_and_observation_cancel_reaps(tmp_path):
    worker = fixture_worker(tmp_path)
    worker.timeout_s = 10
    worker.start()
    lane = ObservationLane(0.05)
    heartbeat = asyncio.create_task(asyncio.sleep(0.01))
    try:
        with pytest.raises(ObservationUnavailable):
            await lane.call(worker.call, "gil_hang", {})
        assert heartbeat.done()
        for _ in range(200):
            if not lane.busy:
                break
            await asyncio.sleep(0.01)
        assert not lane.busy and worker.reaped and worker.state == "FAILED"
    finally:
        worker.close()


def test_invocation_revocation_terminates_compute_without_response(tmp_path):
    worker = fixture_worker(tmp_path)
    worker.timeout_s = 10
    worker.start()
    permit = InvocationPermit(10)
    binding = CURRENT_INVOCATION.set(permit)
    timer = threading.Timer(0.05, permit.revoke)
    timer.start()
    try:
        with pytest.raises(ComputeUnavailable, match="cancel"):
            worker.call("gil_hang", {})
        assert worker.reaped
    finally:
        CURRENT_INVOCATION.reset(binding)
        timer.join()
        worker.close()


def test_already_revoked_caller_does_not_kill_a_healthy_worker(tmp_path):
    worker = fixture_worker(tmp_path)
    worker.start()
    boot = worker.boot_id
    permit = InvocationPermit(10)
    permit.revoke()
    token = CURRENT_INVOCATION.set(permit)
    try:
        with pytest.raises(ComputeUnavailable, match="before_dispatch"):
            worker.call("echo", {"value": "late"})
        assert worker.state == "READY" and worker.boot_id == boot
        assert not (tmp_path / "calls.txt").exists()
    finally:
        CURRENT_INVOCATION.reset(token)
    try:
        assert worker.call("echo", {"value": "fresh"})["value"] == "fresh"
    finally:
        worker.close()
