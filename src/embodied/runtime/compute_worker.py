"""Allowlisted pure-compute worker; stdout is protocol only. 白名单纯计算 worker，标准输出只用于协议。"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys

from embodied.contracts.compute import ComputeEnvelope, encode_frame, json_payload, parse_payload, read_frame
from embodied.runtime.process_tree import bind_parent_death


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent-pid", required=True, type=int)
    args = parser.parse_args()
    bind_parent_death(args.parent_pid)
    from embodied.runtime.invocation import OBSERVATION_ONLY
    OBSERVATION_ONLY.set(True)
    source, sink = sys.stdin.buffer, sys.stdout.buffer
    backend = None
    boot = None
    last_request = 0

    def reply(request, kind, payload):
        message = ComputeEnvelope(boot_id=request.boot_id, request_id=request.request_id, kind=kind,
                                  payload_json=json_payload(payload))
        sink.write(encode_frame(message))
        sink.flush()

    try:
        while True:
            try:
                request = read_frame(source)
            except EOFError:
                break
            if (not request.request_id.isascii() or not request.request_id.isdigit()
                    or request.request_id.startswith("0") or int(request.request_id) <= last_request
                    or boot is not None and request.boot_id != boot):
                raise ValueError("compute identity rejected")
            last_request = int(request.request_id)
            try:
                payload = parse_payload(request.payload_json)
                with contextlib.redirect_stdout(sys.stderr):
                    if backend is None and request.kind == "init":
                        if payload.get("backend") == "render":
                            from embodied.control.render_worker import RenderBackend
                            backend = RenderBackend(payload)
                        else:
                            from embodied.providers.isolated import create_backend
                            backend = create_backend(payload)
                        boot = request.boot_id
                        result, kind = {"pid": os.getpid(), **backend.metadata()}, "ready"
                    elif backend is not None and request.kind == "call":
                        if set(payload) != {"operation", "arguments"} or not isinstance(payload["arguments"], dict):
                            raise ValueError("invalid compute operation")
                        result, kind = backend.execute(payload["operation"], payload["arguments"]), "ok"
                    elif backend is not None and request.kind == "shutdown":
                        backend.close()
                        backend = None
                        reply(request, "ok", {})
                        break
                    else:
                        raise ValueError("invalid compute phase")
                reply(request, kind, result)
            except Exception:
                reply(request, "error", {"reason": "backend_failed"})
                break
    finally:
        if backend is not None:
            with contextlib.redirect_stdout(sys.stderr):
                backend.close()


if __name__ == "__main__":
    main()
