"""Hostile fault-injection fixture for physical control process tests; never a production backend.

物理控制子进程故障注入测试夹具（非生产后端）：支持纯协议故障模式与包装真实 MuJoCo 后端的条件故障触发。
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import json
import os
import struct
import sys
import time
from pathlib import Path

from embodied.contracts.control_process import (
    ControlWorkerEnvelope,
    encode_control_frame,
    json_payload,
    parse_payload,
    read_control_frame,
)
from embodied.runtime.process_tree import bind_parent_death


def _hold_gil_30s() -> None:
    if os.name == "nt":
        ctypes.PyDLL("kernel32").Sleep(30000)
    else:
        ctypes.PyDLL(None).sleep(30)


def _trigger_fault(fault: str, request: ControlWorkerEnvelope, sink) -> bool:
    """Execute the requested hostile fault. Return True if handled without normal reply.

    执行指定故障注入；若已接管响应或终止则返回 True。
    """
    if fault == "hang":
        time.sleep(30)
        return True
    if fault == "gil_hang":
        _hold_gil_30s()
        return True
    if fault == "crash":
        os._exit(23)
    if fault == "abort":
        os.abort()
    if fault == "wrong_boot":
        msg = ControlWorkerEnvelope(
            boot_id="other", request_id=request.request_id, kind="ok", payload_json="{}",
        )
        sink.write(encode_control_frame(msg))
        sink.flush()
        return True
    if fault == "wrong_request":
        msg = ControlWorkerEnvelope(
            boot_id=request.boot_id, request_id="0", kind="ok", payload_json="{}",
        )
        sink.write(encode_control_frame(msg))
        sink.flush()
        return True
    if fault == "oversize":
        sink.write(struct.pack("!I", 100_000_000))
        sink.flush()
        return True
    if fault == "error":
        msg = ControlWorkerEnvelope(
            boot_id=request.boot_id,
            request_id=request.request_id,
            kind="error",
            payload_json=json_payload({"reason": "injected_error"}),
        )
        sink.write(encode_control_frame(msg))
        sink.flush()
        return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["stub", "real"], default="stub")
    parser.add_argument("--fault-file", default="")
    parser.add_argument("--trace", default="")
    args, _ = parser.parse_known_args()

    bind_parent_death(os.getppid())
    if os.name != "nt":
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    source, sink = sys.stdin.buffer, sys.stdout.buffer
    backend = None
    config: dict = {}
    op_counts: dict[str, int] = {}

    def send(request: ControlWorkerEnvelope, kind: str, payload: dict) -> None:
        msg = ControlWorkerEnvelope(
            boot_id=request.boot_id,
            request_id=request.request_id,
            kind=kind,  # type: ignore[arg-type]
            payload_json=json_payload(payload),
        )
        sink.write(encode_control_frame(msg))
        sink.flush()

    def _next_fault(operation: str) -> str:
        # 仅在故障控制文件存在且命中指定操作与次数时触发一次（用于真实 MuJoCo 中途注入）。
        # Trigger once when the fault control file matches operation and count (for mid-skill MuJoCo faults).
        if not args.fault_file:
            return ""
        fpath = Path(args.fault_file)
        if not fpath.exists():
            return ""
        spec = json.loads(fpath.read_text(encoding="utf-8"))
        if spec.get("operation") != operation:
            return ""
        count = op_counts.get(operation, 0)
        if count < int(spec.get("after", 0)):
            return ""
        if spec.get("once", True):
            try:
                fpath.unlink()
            except OSError:
                pass
        return str(spec.get("fault", ""))

    try:
        while True:
            try:
                request = read_control_frame(source)
            except EOFError:
                break
            payload = parse_payload(request.payload_json)
            if request.kind == "init":
                config = payload
                if args.mode == "real":
                    with contextlib.redirect_stdout(sys.stderr):
                        from embodied.control.control_worker import PhysicalControlBackend

                        backend = PhysicalControlBackend(payload)
                        meta = backend.metadata()
                    send(request, "ready", meta)
                else:
                    if config.get("startup_hang"):
                        time.sleep(30)
                    send(
                        request,
                        "ready",
                        {"pid": os.getpid(), "has_secret": "CONTROL_TEST_SECRET" in os.environ},
                    )
                continue

            if request.kind == "shutdown":
                if backend is not None:
                    with contextlib.redirect_stdout(sys.stderr):
                        backend.close()
                    backend = None
                send(request, "ok", {})
                break

            operation = str(payload["operation"])
            arguments = payload["arguments"]
            op_counts[operation] = op_counts.get(operation, 0) + 1
            trace_path = args.trace or config.get("trace", "")
            if trace_path:
                with Path(trace_path).open("a", encoding="utf-8") as fh:
                    fh.write(f"{operation}:{arguments.get('reason', '')}\n")

            if args.mode == "stub":
                fault = str(arguments.get("fault", ""))
                if fault and _trigger_fault(fault, request, sink):
                    continue
                if operation == "estop":
                    send(request, "ok", {"guard_halted": True, "reason": arguments.get("reason", ""), "pid": os.getpid()})
                else:
                    send(request, "ok", {"op": operation, "pid": os.getpid(), "args": arguments})
            else:
                assert backend is not None
                fault = _next_fault(operation)
                if fault and _trigger_fault(fault, request, sink):
                    continue
                with contextlib.redirect_stdout(sys.stderr):
                    res = backend.execute(operation, arguments)
                send(request, "ok", res)
    finally:
        if backend is not None:
            with contextlib.redirect_stdout(sys.stderr):
                backend.close()


if __name__ == "__main__":
    main()
