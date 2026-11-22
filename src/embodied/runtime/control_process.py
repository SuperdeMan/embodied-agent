"""Single-flight subprocess supervisor for isolated physical control (D034).

隔离物理控制子进程的单在途监督器（D034）：提供有界调用、紧急急停强杀回收与显式代次重启。
"""

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
from typing import Any, Callable
from uuid import uuid4

from embodied.contracts.control_process import (
    ALLOWED_CONTROL_OPERATIONS,
    ControlWorkerEnvelope,
    encode_control_frame,
    json_payload,
    parse_payload,
    read_control_frame,
)
from embodied.runtime.compute import worker_environment
from embodied.runtime.invocation import CURRENT_INVOCATION, OBSERVATION_CANCELLED
from embodied.runtime.observation import ObservationUnavailable
from embodied.runtime.process_tree import WindowsJob, terminate_tree

_CONTROL_WORKERS = weakref.WeakSet()


class PhysicalControlUnavailable(ObservationUnavailable):
    """Raised when the isolated physical control process fails, hangs, or is closed.

    隔离物理控制子进程故障、挂起或关闭时抛出的不可用异常。
    """


class ControlProcess:
    """Supervises one isolated physical control child process with strict reaping.

    监管单个隔离物理控制子进程，负责帧身份校验、有界等待、紧急强杀与进程树回收。
    """

    def __init__(
        self,
        config: dict[str, Any],
        *,
        timeout_s: float = 5.0,
        startup_timeout_s: float = 30.0,
        cancel_grace_s: float = 0.15,
        halt_timeout_s: float = 0.5,
        on_fault: Callable[[str], None] | None = None,
        _command: list[str] | None = None,
    ) -> None:
        if any(
            not math.isfinite(val) or val <= 0
            for val in (timeout_s, startup_timeout_s, cancel_grace_s, halt_timeout_s)
        ):
            raise ValueError("physical control budgets must be finite and positive")
        self._config_json = json_payload(parse_payload(json_payload(config)))
        self._environment = worker_environment()
        self.timeout_s = float(timeout_s)
        self.startup_timeout_s = float(startup_timeout_s)
        self.cancel_grace_s = float(cancel_grace_s)
        self.halt_timeout_s = float(halt_timeout_s)
        self._on_fault = on_fault
        self._command = list(_command) if _command is not None else None

        # _lock 串行化单在途请求；_proc_lock 仅保护底层进程/Job 句柄以便跨线程紧急终止。
        # _lock serializes single-flight calls; _proc_lock guards only OS handles for cross-thread urgent kill.
        self._lock = threading.Lock()
        self._proc_lock = threading.Lock()
        self._stop = threading.Event()
        self._abort_event = threading.Event()
        self._jobs: queue.Queue = queue.Queue(maxsize=1)
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen | None = None
        self._job: WindowsJob | None = None
        self._ready: Future | None = None

        self.boot_id = ""
        self.state = "NEW"
        self.metadata: dict[str, Any] = {}
        self.last_pid: int | None = None
        self.last_exitcode: int | None = None
        self.last_fault_reason = ""
        self.shutdown_acknowledged = False
        self._sequence = 0
        _CONTROL_WORKERS.add(self)

    @property
    def config(self) -> dict[str, Any]:
        return parse_payload(self._config_json)

    @property
    def reaped(self) -> bool:
        with self._proc_lock:
            proc = self._process
            thread = self._thread
        return (proc is None or proc.returncode is not None) and (thread is None or not thread.is_alive())

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    def _request(self, kind: str, payload: str) -> ControlWorkerEnvelope:
        self._sequence += 1
        return ControlWorkerEnvelope(
            boot_id=self.boot_id,
            request_id=str(self._sequence),
            kind=kind,  # type: ignore[arg-type]
            payload_json=payload,
        )

    def _exchange(self, request: ControlWorkerEnvelope, expected: str) -> dict[str, Any]:
        with self._proc_lock:
            proc = self._process
        if proc is None or proc.stdin is None or proc.stdout is None:
            raise PhysicalControlUnavailable("physical_control_pipe_missing")
        data = memoryview(encode_control_frame(request))
        while data:
            written = proc.stdin.write(data)
            if not written:
                raise EOFError("control worker input closed")
            data = data[written:]
        proc.stdin.flush()
        reply = read_control_frame(proc.stdout)
        if reply.boot_id != request.boot_id or reply.request_id != request.request_id:
            raise PhysicalControlUnavailable("physical_control_identity_mismatch")
        payload = parse_payload(reply.payload_json)
        if reply.kind == "error":
            reason = str(payload.get("reason", "backend_failed"))
            raise PhysicalControlUnavailable(f"physical_control_backend_failed:{reason}")
        if reply.kind != expected:
            raise PhysicalControlUnavailable("physical_control_reply_kind_mismatch")
        return payload

    def _pump(self) -> None:
        current = self._ready
        proc: subprocess.Popen | None = None
        try:
            if self._stop.is_set() or self._abort_event.is_set():
                raise PhysicalControlUnavailable("physical_control_aborted")
            # 直接启动基础解释器并注入包路径，规避 Windows venv 重定向器产生无绑定子 PID。
            # Launch the base interpreter directly with package site injection to avoid Windows venv redirector PIDs.
            bootstrap = (
                "import sys,site,runpy; p=sys.argv.pop(1); sys.path.insert(0,p); site.addsitedir(p); "
                "sys.argv[0]='embodied.control.control_worker'; "
                "runpy.run_module('embodied.control.control_worker',run_name='__main__')"
            )
            command = self._command or [
                getattr(sys, "_base_executable", sys.executable),
                "-I",
                "-u",
                "-c",
                bootstrap,
                str(importlib.metadata.distribution("embodied-agent").locate_file("")),
                "--parent-pid",
                str(os.getpid()),
            ]
            proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
                close_fds=True,
                env=self._environment,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                start_new_session=os.name != "nt",
            )
            job = WindowsJob(proc.pid) if os.name == "nt" else None
            with self._proc_lock:
                self._process = proc
                self._job = job
                self.last_pid = proc.pid
            if self._stop.is_set() or self._abort_event.is_set():
                raise PhysicalControlUnavailable("physical_control_aborted")
            request = self._request("init", json_payload(self.config))
            metadata = self._exchange(request, "ready")
            if metadata.get("pid") != proc.pid:
                raise PhysicalControlUnavailable("physical_control_pid_mismatch")
            if current is not None:
                current.set_result(metadata)
            while not self._stop.is_set() and not self._abort_event.is_set():
                try:
                    current, request, expected = self._jobs.get(timeout=0.05)
                except queue.Empty:
                    continue
                current.set_result(self._exchange(request, expected))
                if request.kind == "shutdown":
                    # Linux 保持创建线程存活至子进程优雅退出，避免 PR_SET_PDEATHSIG 抢先触发。
                    # Keep the creating thread alive on Linux until graceful child exit so PDEATHSIG does not preempt it.
                    deadline = time.monotonic() + 0.5
                    while not self._stop.is_set() and not self._abort_event.is_set() and time.monotonic() < deadline:
                        if proc.returncode is not None:
                            break
                        if os.name != "nt" and hasattr(os, "WNOWAIT"):
                            try:
                                if os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None:
                                    break
                            except ChildProcessError:
                                break
                        time.sleep(0.01)
                    break
        except BaseException as exc:
            if current is not None and not current.done():
                reason = (
                    str(exc)
                    if isinstance(exc, PhysicalControlUnavailable)
                    else f"physical_control_transport_failed:{type(exc).__name__}"
                )
                current.set_exception(PhysicalControlUnavailable(reason))
        finally:
            if proc is not None:
                for stream in (proc.stdin, proc.stdout):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            pass

    @staticmethod
    def _external_cancelled() -> bool:
        token = OBSERVATION_CANCELLED.get()
        if token is not None and token.is_set():
            return True
        permit = CURRENT_INVOCATION.get()
        if permit is not None:
            with permit.access() as allowed:
                return not allowed
        return False

    def _wait(self, future: Future, timeout: float, *, check_external_cancel: bool = True) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        cancelled_at: float | None = None
        while True:
            if self._stop.is_set() or self._abort_event.is_set():
                raise PhysicalControlUnavailable("physical_control_aborted")
            now = time.monotonic()
            if check_external_cancel and self._external_cancelled():
                if cancelled_at is None:
                    cancelled_at = now
                if now - cancelled_at >= self.cancel_grace_s:
                    raise PhysicalControlUnavailable("physical_control_cancelled_timeout")
            remaining = deadline - now
            if remaining <= 0:
                raise PhysicalControlUnavailable("physical_control_timeout")
            wait_slice = min(0.01, remaining)
            if cancelled_at is not None:
                grace_left = (cancelled_at + self.cancel_grace_s) - now
                if grace_left <= 0:
                    raise PhysicalControlUnavailable("physical_control_cancelled_timeout")
                wait_slice = min(wait_slice, grace_left)
            try:
                value = future.result(timeout=wait_slice)
                if self._stop.is_set() or self._abort_event.is_set():
                    raise PhysicalControlUnavailable("physical_control_aborted")
                if time.monotonic() >= deadline:
                    raise PhysicalControlUnavailable("physical_control_late_response")
                return value
            except TimeoutError:
                continue

    def _start_locked(self) -> dict[str, Any]:
        if self.state == "READY":
            return self.metadata
        if self.state == "CLOSED":
            raise PhysicalControlUnavailable("physical_control_closed")
        if self.state != "NEW":
            raise PhysicalControlUnavailable("physical_control_requires_explicit_restart")
        self._stop.clear()
        self._abort_event.clear()
        self._jobs = queue.Queue(maxsize=1)
        self.boot_id = uuid4().hex
        self._sequence = 0
        self._ready = Future()
        thread = threading.Thread(target=Context().run, args=(self._pump,), name="control-io", daemon=True)
        with self._proc_lock:
            self._thread = thread
        thread.start()
        self.metadata = self._wait(self._ready, self.startup_timeout_s, check_external_cancel=True)
        self.state = "READY"
        return self.metadata

    def start(self) -> dict[str, Any]:
        """Start the child physical control process if NEW; refuse FAILED/CLOSED.

        在 NEW 状态下启动物理控制子进程；FAILED 或 CLOSED 拒绝静默拉起。
        """
        if not self._lock.acquire(blocking=False):
            raise PhysicalControlUnavailable("physical_control_busy")
        try:
            try:
                return self._start_locked()
            except BaseException as exc:
                if self.state != "CLOSED":
                    self._fail_locked(str(exc) or type(exc).__name__)
                raise
        finally:
            self._lock.release()

    def _call_locked(
        self,
        operation: str,
        arguments: dict[str, Any],
        *,
        timeout_s: float | None = None,
        validate: Callable[[dict[str, Any]], Any] | None = None,
        check_external_cancel: bool = True,
    ) -> Any:
        if operation not in ALLOWED_CONTROL_OPERATIONS:
            raise ValueError(f"control operation not allowed: {operation!r}")
        budget = self.timeout_s if timeout_s is None else float(timeout_s)
        if not math.isfinite(budget) or budget <= 0:
            raise ValueError("control call timeout must be finite and positive")
        encoded = json_payload({"operation": operation, "arguments": arguments})
        if self.state != "READY":
            if self.state == "CLOSED":
                raise PhysicalControlUnavailable("physical_control_closed")
            if self.state == "FAILED":
                raise PhysicalControlUnavailable("physical_control_requires_explicit_restart")
            self._start_locked()
        request = self._request("call", encoded)
        encode_control_frame(request)
        future: Future = Future()
        self._jobs.put_nowait((future, request, "ok"))
        result = self._wait(future, budget, check_external_cancel=check_external_cancel)
        return validate(result) if validate is not None else result

    def call(
        self,
        operation: str,
        arguments: dict[str, Any],
        *,
        timeout_s: float | None = None,
        validate: Callable[[dict[str, Any]], Any] | None = None,
    ) -> Any:
        """Execute one whitelisted control operation with bounded timeout and fault reaping.

        执行单次白名单物理控制操作，超时或故障立即回收子进程并进入 FAILED。
        """
        if operation not in ALLOWED_CONTROL_OPERATIONS:
            raise ValueError(f"control operation not allowed: {operation!r}")
        if not self._lock.acquire(blocking=False):
            raise PhysicalControlUnavailable("physical_control_busy")
        try:
            try:
                return self._call_locked(operation, arguments, timeout_s=timeout_s, validate=validate)
            except BaseException as exc:
                if self.state != "CLOSED":
                    self._fail_locked(str(exc) or type(exc).__name__)
                raise
        finally:
            self._lock.release()

    def _kill_handles(self) -> None:
        with self._proc_lock:
            proc = self._process
            job = self._job
            self._job = None
        if proc is not None:
            terminate_tree(proc, job)
            for stream in (proc.stdin, proc.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
        elif job is not None:
            job.close()

    def _reap_locked(self) -> None:
        self._stop.set()
        self._abort_event.set()
        deadline = time.monotonic() + 2.0
        while not self.reaped and time.monotonic() < deadline:
            with self._proc_lock:
                proc = self._process
                job = self._job
                self._job = None
                thread = self._thread
            if proc is not None and proc.poll() is None:
                terminate_tree(proc, job)
                try:
                    proc.wait(timeout=max(0.05, min(0.25, deadline - time.monotonic())))
                except subprocess.TimeoutExpired:
                    pass
            elif job is not None:
                job.close()
            if proc is not None and proc.returncode is not None:
                self.last_exitcode = proc.returncode
            if thread is not None and thread.is_alive():
                thread.join(timeout=max(0.05, min(0.25, deadline - time.monotonic())))
        if self.reaped:
            with self._proc_lock:
                if self._process is not None:
                    self.last_exitcode = self._process.returncode
                self._process = None
                self._thread = None
                self._job = None

    def _fail_locked(self, reason: str) -> None:
        already_failed = self.state == "FAILED"
        self.state = "FAILED"
        self.last_fault_reason = reason
        self._reap_locked()
        if not already_failed and self._on_fault is not None:
            try:
                self._on_fault(reason)
            except Exception:
                pass

    def urgent_estop(self, reason: str) -> bool:
        """Synchronize estop to a healthy worker, or force-kill a hung worker tree.

        健康子进程同步闩锁急停；若子进程原生挂起超宽限窗口，则立即强杀并回收子进程树。
        """
        if self.state != "READY":
            return False
        acquired = self._lock.acquire(timeout=self.cancel_grace_s)
        if acquired:
            try:
                if self.state != "READY":
                    return False
                try:
                    self._call_locked(
                        "estop",
                        {"reason": str(reason)},
                        timeout_s=self.halt_timeout_s,
                        check_external_cancel=False,
                    )
                    return True
                except BaseException as exc:
                    if self.state != "CLOSED":
                        self._fail_locked(f"estop_sync_failed:{exc}")
                    return False
            finally:
                self._lock.release()
        # 持锁者在宽限窗口内未返回，判定原生挂起：跨线程直接终止子进程树唤醒等待方。
        # Caller held the lock past grace window -> native hang: kill the child tree cross-thread to unblock.
        self._abort_event.set()
        self._kill_handles()
        deadline = time.monotonic() + 2.0
        while not self.reaped and time.monotonic() < deadline:
            time.sleep(0.01)
        if self._lock.acquire(timeout=0.5):
            try:
                if self.state not in {"FAILED", "CLOSED"}:
                    self._fail_locked(f"urgent_estop_killed_hung_worker:{reason}")
            finally:
                self._lock.release()
        return False

    def request_abort(self) -> None:
        """Signal any in-flight call to abort and kill a blocked child process tree.

        通知在途调用中止并立即终止阻塞中的物理子进程树，用于受控关闭。
        """
        self._abort_event.set()
        self._kill_handles()

    def restart(self) -> dict[str, Any]:
        """Explicitly launch a new worker generation after FAILED + confirmed reaping.

        仅在 FAILED 且确认已完全回收后显式启动新代次，绝不自动重放旧请求。
        """
        with self._lock:
            if self.state == "CLOSED":
                raise PhysicalControlUnavailable("physical_control_closed")
            if self.state != "FAILED":
                raise PhysicalControlUnavailable("physical_control_not_failed")
            self._reap_locked()
            if not self.reaped:
                raise PhysicalControlUnavailable("physical_control_still_running")
            self._stop = threading.Event()
            self._abort_event = threading.Event()
            self._jobs = queue.Queue(maxsize=1)
            self.metadata = {}
            self.last_exitcode = None
            self.shutdown_acknowledged = False
            self.state = "NEW"
            try:
                return self._start_locked()
            except BaseException as exc:
                self._fail_locked(str(exc) or type(exc).__name__)
                raise

    def close(self) -> None:
        """Gracefully shut down a READY worker or forcibly reap a blocked/FAILED one.

        优雅关闭健康子进程，或强制回收阻塞/故障子进程树并进入终态 CLOSED。
        """
        acquired = self._lock.acquire(timeout=self.cancel_grace_s)
        if not acquired:
            self._abort_event.set()
            self._kill_handles()
            acquired = self._lock.acquire(timeout=2.0)
        try:
            self.shutdown_acknowledged = False
            if self.state == "READY" and acquired:
                try:
                    future: Future = Future()
                    self._jobs.put_nowait((future, self._request("shutdown", "{}"), "ok"))
                    future.result(timeout=1.0)
                    self.shutdown_acknowledged = True
                    with self._proc_lock:
                        proc = self._process
                        thread = self._thread
                    if proc is not None:
                        proc.wait(timeout=1.0)
                        self.last_exitcode = proc.returncode
                    if thread is not None:
                        thread.join(timeout=1.0)
                except Exception:
                    pass
            self.state = "CLOSED"
            self._reap_locked()
            if not self.reaped:
                raise PhysicalControlUnavailable("physical_control_close_incomplete")
        finally:
            if acquired:
                self._lock.release()


def _cleanup_control_workers() -> None:
    for worker in list(_CONTROL_WORKERS):
        try:
            worker.close()
        except Exception:
            pass


atexit.register(_cleanup_control_workers)
