"""Hostile protocol fixture, never a production backend. 故障协议夹具，不是生产后端。"""

import ctypes
import os
import struct
import subprocess
import sys
import time
from pathlib import Path

from embodied.contracts.compute import ComputeEnvelope, encode_frame, json_payload, parse_payload, read_frame
from embodied.runtime.process_tree import bind_parent_death

bind_parent_death(os.getppid())
if os.name != "nt":
    import resource
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
source, sink = sys.stdin.buffer, sys.stdout.buffer
config = {}


def send(request, kind, payload, **changes):
    values = {"boot_id": request.boot_id, "request_id": request.request_id, "kind": kind,
              "payload_json": json_payload(payload)}
    values.update(changes)
    sink.write(encode_frame(ComputeEnvelope(**values)))
    sink.flush()


while True:
    try:
        request = read_frame(source)
    except EOFError:
        break
    payload = parse_payload(request.payload_json)
    if request.kind == "init":
        config = payload
        if config.get("startup_hang"):
            time.sleep(30)
        send(request, "ready", {"pid": os.getpid(), "has_secret": "COMPUTE_TEST_SECRET" in os.environ})
        continue
    if request.kind == "shutdown":
        if config.get("shutdown_hang"):
            time.sleep(30)
        send(request, "ok", {})
        break
    operation = payload["operation"]
    if config.get("trace"):
        with Path(config["trace"]).open("a", encoding="utf-8") as file:
            file.write(operation + "\n")
    if operation == "hang":
        time.sleep(30)
    elif operation == "gil_hang":
        if os.name == "nt":
            ctypes.PyDLL("kernel32").Sleep(30000)
        else:
            ctypes.PyDLL(None).sleep(30)
    elif operation == "crash":
        os._exit(23)
    elif operation == "abort":
        os.abort()
    elif operation == "wrong_boot":
        send(request, "ok", {}, boot_id="other")
    elif operation == "wrong_request":
        send(request, "ok", {}, request_id="0")
    elif operation == "oversize":
        sink.write(struct.pack("!I", 100_000_000))
        sink.flush()
    elif operation == "partial":
        sink.write(struct.pack("!I", 20) + b"{}")
        sink.flush()
        os._exit(24)
    elif operation == "bad_json":
        sink.write(struct.pack("!I", 3) + b"bad")
        sink.flush()
    elif operation == "spawn":
        child = subprocess.Popen([getattr(sys, "_base_executable", sys.executable), "-c", "import time;time.sleep(60)"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        send(request, "ok", {"pid": child.pid})
    else:
        send(request, "ok", {"value": payload["arguments"].get("value"), "pid": os.getpid()})
