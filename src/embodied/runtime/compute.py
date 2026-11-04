"""Single-flight subprocess supervisor for pure computation. 纯计算的单在途子进程监督器。"""

from __future__ import annotations

import atexit
import importlib.metadata
import math
import os
import queue
import subprocess
import sys
import threading
import time
import weakref
from concurrent.futures import Future, TimeoutError
from contextvars import Context
from uuid import uuid4

from embodied.contracts.compute import ComputeEnvelope, encode_frame, json_payload, parse_payload, read_frame
from embodied.runtime.invocation import CURRENT_INVOCATION, OBSERVATION_CANCELLED
from embodied.runtime.observation import ObservationUnavailable
from embodied.runtime.process_tree import WindowsJob, terminate_tree

_WORKERS = weakref.WeakSet()


class ComputeUnavailable(ObservationUnavailable):
    pass


def worker_environment() -> dict[str, str]:
    names = {"PATH", "SystemRoot", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE", "LOCALAPPDATA",
             "APPDATA", "LANG", "LC_ALL", "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "MUJOCO_GL", "PYOPENGL_PLATFORM",
             "HF_HOME", "HUGGINGFACE_HUB_CACHE", "XDG_CACHE_HOME"}
    env = {key: value for key, value in os.environ.items() if key in names}
    env.update(OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1", LP_NUM_THREADS="1")
    return env


class ComputeProcess:
    def __init__(self, config: dict, *, timeout_s: float = 2.0, startup_timeout_s: float = 30.0, _command=None):
        if any(not math.isfinite(t) or t <= 0 for t in (timeout_s, startup_timeout_s)):
            raise ValueError("compute budgets must be finite and positive")
        self.config = parse_payload(json_payload(config))
        self.timeout_s, self.startup_timeout_s = timeout_s, startup_timeout_s
        self._command = _command
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._jobs = queue.Queue(maxsize=1)
        self._thread = None
        self._process = None
        self._job = None
        self._ready = None
        self.boot_id = ""
        self.state = "NEW"
        self.metadata = {}
        self.last_pid = None
        self.last_exitcode = None
        self.shutdown_acknowledged = False
        self._sequence = 0
        _WORKERS.add(self)

    @property
    def reaped(self) -> bool:
        return (self._process is None or self._process.returncode is not None) and (self._thread is None or not self._thread.is_alive())

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    def request_stop(self):
        self._stop.set()

    def _request(self, kind, payload):
        self._sequence += 1
        return ComputeEnvelope(boot_id=self.boot_id, request_id=str(self._sequence), kind=kind, payload_json=payload)

    def _exchange(self, request, expected):
        proc = self._process
        data = memoryview(encode_frame(request))
        while data:
            written = proc.stdin.write(data)
            if not written:
                raise EOFError("compute input closed")
            data = data[written:]
        proc.stdin.flush()
        reply = read_frame(proc.stdout)
        if reply.boot_id != request.boot_id or reply.request_id != request.request_id:
            raise ComputeUnavailable("compute_identity_mismatch")
        payload = parse_payload(reply.payload_json)
        if reply.kind == "error":
            raise ComputeUnavailable("compute_backend_failed")
        if reply.kind != expected:
            raise ComputeUnavailable("compute_reply_kind_mismatch")
        return payload

    def _pump(self):
        current = self._ready
        try:
            if self._stop.is_set():
                raise ComputeUnavailable("compute_cancelled")
            # 直接启动解释器，避开 Windows venv 重定向器产生未绑定的第二个 PID。
            # Launch the interpreter directly; avoid an unbound second PID from the Windows venv redirector.
            bootstrap = ("import sys,site,runpy; p=sys.argv.pop(1); sys.path.insert(0,p); site.addsitedir(p); "
                         "sys.argv[0]='embodied.runtime.compute_worker'; "
                         "runpy.run_module('embodied.runtime.compute_worker',run_name='__main__')")
            command = self._command or [getattr(sys, "_base_executable", sys.executable), "-I", "-u", "-c", bootstrap,
                                        str(importlib.metadata.distribution("embodied-agent").locate_file("")),
                                        "--parent-pid", str(os.getpid())]
            self._process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                bufsize=0, close_fds=True, env=worker_environment(),
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                start_new_session=os.name != "nt",
            )
            self.last_pid = self._process.pid
            if os.name == "nt":
                self._job = WindowsJob(self._process.pid)
            if self._stop.is_set():
                raise ComputeUnavailable("compute_cancelled")
            request = self._request("init", json_payload(self.config))
            metadata = self._exchange(request, "ready")
            if metadata.get("pid") != self._process.pid:
                raise ComputeUnavailable("compute_pid_mismatch")
            current.set_result(metadata)
            while not self._stop.is_set():
                try:
                    current, request, expected = self._jobs.get(timeout=0.05)
                except queue.Empty:
                    continue
                current.set_result(self._exchange(request, expected))
                if request.kind == "shutdown":
                    # Linux 父线程死亡信号不能抢在子进程的正常退出前触发。
                    # Keep the creating thread alive until graceful child exit on Linux.
                    deadline = time.monotonic() + 0.5
                    while not self._stop.is_set() and time.monotonic() < deadline:
                        if self._process.returncode is not None:
                            break
                        if os.name != "nt" and hasattr(os, "WNOWAIT"):
                            try:
                                if os.waitid(os.P_PID, self._process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None:
                                    break
                            except ChildProcessError:
                                break
                        time.sleep(0.01)
                    break
        except BaseException as exc:
            if current is not None and not current.done():
                reason = str(exc) if isinstance(exc, ComputeUnavailable) else "compute_transport_failed:" + type(exc).__name__
                current.set_exception(ComputeUnavailable(reason))
        finally:
            if self._process is not None:
                for stream in (self._process.stdin, self._process.stdout):
                    try:
                        stream.close()
                    except OSError:
                        pass

    def _external_cancelled(self):
        token = OBSERVATION_CANCELLED.get()
        if token is not None and token.is_set():
            return True
        permit = CURRENT_INVOCATION.get()
        if permit is not None:
            with permit.access() as allowed:
                return not allowed
        return False

    def _cancelled(self):
        return self._stop.is_set() or self._external_cancelled()

    def _wait(self, future, timeout):
        deadline = time.monotonic() + timeout
        while True:
            if self._cancelled():
                raise ComputeUnavailable("compute_cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ComputeUnavailable("compute_timeout")
            try:
                value = future.result(timeout=min(0.02, remaining))
                if self._cancelled() or time.monotonic() >= deadline:
                    raise ComputeUnavailable("compute_late_response")
                return value
            except TimeoutError:
                continue

    def _start(self):
        if self.state == "READY":
            return self.metadata
        if self.state == "CLOSED":
            raise ComputeUnavailable("compute_closed")
        if self.state != "NEW":
            raise ComputeUnavailable("compute_requires_explicit_restart")
        self.boot_id = uuid4().hex
        self._sequence = 0
        self._ready = Future()
        self._thread = threading.Thread(target=Context().run, args=(self._pump,), name="compute-io", daemon=True)
        self._thread.start()
        self.metadata = self._wait(self._ready, self.startup_timeout_s)
        self.state = "READY"
        return self.metadata

    def start(self):
        if not self._lock.acquire(blocking=False):
            raise ComputeUnavailable("compute_busy")
        try:
            if self._external_cancelled():
                raise ComputeUnavailable("compute_cancelled_before_dispatch")
            try:
                return self._start()
            except BaseException:
                if self.state != "CLOSED":
                    self._fail()
                raise
        finally:
            self._lock.release()

    def call(self, operation: str, payload: dict, *, validate=None):
        encoded = json_payload({"operation": operation, "arguments": payload})
        if not self._lock.acquire(blocking=False):
            raise ComputeUnavailable("compute_busy")
        try:
            if self._external_cancelled():
                raise ComputeUnavailable("compute_cancelled_before_dispatch")
            try:
                self._start()
                request = self._request("call", encoded)
                encode_frame(request)
                future = Future()
                self._jobs.put_nowait((future, request, "ok"))
                result = self._wait(future, self.timeout_s)
                return validate(result) if validate is not None else result
            except BaseException:
                if self.state != "CLOSED":
                    self._fail()
                raise
        finally:
            self._lock.release()

    def _reap(self):
        self._stop.set()
        deadline = time.monotonic() + 2.0
        while not self.reaped and time.monotonic() < deadline:
            process = self._process
            if process is not None:
                if os.name == "nt" or process.returncode is None:
                    terminate_tree(process, self._job)
                try:
                    self.last_exitcode = process.wait(timeout=0.05)
                except subprocess.TimeoutExpired:
                    pass
            if self._thread is not None:
                self._thread.join(timeout=0.05)
        if self._job is not None:
            self._job.close()
        if not self.reaped:
            raise ComputeUnavailable("compute_not_reaped")

    def _fail(self):
        self.state = "FAILED"
        self._reap()

    def restart(self):
        if not self._lock.acquire(blocking=False):
            raise ComputeUnavailable("compute_busy")
        try:
            if self.state != "FAILED" or not self.reaped:
                raise ComputeUnavailable("compute_restart_not_ready")
            self._stop = threading.Event()
            self._jobs = queue.Queue(maxsize=1)
            self._thread = self._process = self._job = None
            self.state = "NEW"
            try:
                return self._start()
            except BaseException:
                self._fail()
                raise
        finally:
            self._lock.release()

    def close(self):
        if self.state == "CLOSED":
            return
        if not self._lock.acquire(blocking=False):
            self._stop.set()
            if not self._lock.acquire(timeout=3.0):
                raise ComputeUnavailable("compute_close_in_progress")
        try:
            if self.state == "READY" and self._process.returncode is None:
                request = self._request("shutdown", "{}")
                future = Future()
                self._jobs.put_nowait((future, request, "ok"))
                try:
                    future.result(timeout=0.25)
                    self.shutdown_acknowledged = True
                    if os.name == "nt":
                        self.last_exitcode = self._process.wait(timeout=0.25)
                    elif hasattr(os, "WNOWAIT"):
                        # 保留 leader PID 直到进程组清理，避免 PID 复用误伤其他进程。
                        # Keep the leader PID unreaped until group cleanup to avoid PID-reuse hazards.
                        deadline = time.monotonic() + 0.25
                        while time.monotonic() < deadline:
                            if os.waitid(os.P_PID, self._process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None:
                                break
                            time.sleep(0.01)
                except Exception:
                    pass
            self._reap()
            self.state = "CLOSED"
        finally:
            self._lock.release()


@atexit.register
def _close_workers():
    for worker in list(_WORKERS):
        try:
            worker.close()
        except Exception:
            pass
