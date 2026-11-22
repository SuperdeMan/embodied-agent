"""Bounded JSON pipe contract for isolated physical control (D034).

隔离物理控制子进程的有界 JSON 管道契约（D034），字段源自 control_worker.proto。
"""

from __future__ import annotations

import struct
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from embodied.contracts.compute import MAX_FRAME_BYTES, json_payload, parse_payload

ALLOWED_CONTROL_OPERATIONS: frozenset[str] = frozenset({
    "reset",
    "read",
    "read_control_feedback",
    "write",
    "step",
    "solve_reach",
    "render_snapshot",
    "camera_model",
    "estop",
    "guard_reset",
    "stop_prepare",
    "stop_commit",
    "stop_abort",
})


class ControlWorkerEnvelope(BaseModel):
    """Framed control-worker message mirroring control_worker.proto.

    与 control_worker.proto 一一对应的物理控制子进程帧消息。
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    api_version: Literal["embodied.dev/v1alpha1"] = "embodied.dev/v1alpha1"
    boot_id: str = Field(min_length=1, max_length=64)
    request_id: str = Field(min_length=1, max_length=64)
    kind: Literal["init", "ready", "call", "ok", "error", "shutdown"]
    payload_json: str


def encode_control_frame(envelope: ControlWorkerEnvelope) -> bytes:
    """Serialize an envelope with a 4-byte network-order uint32 length prefix.

    使用 4 字节网络序 uint32 长度前缀序列化控制帧。
    """
    data = envelope.model_dump_json().encode("utf-8")
    if len(data) > MAX_FRAME_BYTES:
        raise ValueError("control worker frame too large")
    return struct.pack("!I", len(data)) + data


def _read_exact(stream, size: int) -> bytes:
    parts = bytearray()
    while len(parts) < size:
        chunk = stream.read(size - len(parts))
        if not chunk:
            raise EOFError("control worker pipe closed")
        parts.extend(chunk)
    return bytes(parts)


def read_control_frame(stream) -> ControlWorkerEnvelope:
    """Read and strictly validate one framed control-worker message.

    读取并严格校验单条物理控制子进程帧消息。
    """
    size, = struct.unpack("!I", _read_exact(stream, 4))
    if not 0 < size <= MAX_FRAME_BYTES:
        raise ValueError("invalid control worker frame length")
    raw = _read_exact(stream, size).decode("utf-8")
    value = parse_payload(raw)
    if set(value) != set(ControlWorkerEnvelope.model_fields):
        raise ValueError("missing or unknown control worker envelope field")
    return ControlWorkerEnvelope.model_validate(value)


__all__ = [
    "ALLOWED_CONTROL_OPERATIONS",
    "ControlWorkerEnvelope",
    "MAX_FRAME_BYTES",
    "encode_control_frame",
    "json_payload",
    "parse_payload",
    "read_control_frame",
]
