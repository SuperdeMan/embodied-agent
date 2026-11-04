"""Bounded JSON pipe contract declared in compute.proto. compute.proto 声明的有界 JSON 管道契约。"""

from __future__ import annotations

import base64
import json
import math
import struct
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_FRAME_BYTES = 16 * 1024 * 1024
MAX_ARRAY_BYTES = 8 * 1024 * 1024


class ComputeEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    api_version: Literal["embodied.dev/v1alpha1"] = "embodied.dev/v1alpha1"
    boot_id: str = Field(min_length=1, max_length=64)
    request_id: str = Field(min_length=1, max_length=64)
    kind: Literal["init", "ready", "call", "ok", "error", "shutdown"]
    payload_json: str


def json_payload(value) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def parse_payload(text: str) -> dict:
    def invalid(value):
        raise ValueError("nonfinite JSON constant")
    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("nonfinite JSON number")
        return number
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result
    value = json.loads(text, parse_constant=invalid, parse_float=finite_float, object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError("payload must be an object")
    return value


def encode_frame(envelope: ComputeEnvelope) -> bytes:
    data = envelope.model_dump_json().encode()
    if len(data) > MAX_FRAME_BYTES:
        raise ValueError("compute frame too large")
    return struct.pack("!I", len(data)) + data


def _read_exact(stream, size):
    parts = bytearray()
    while len(parts) < size:
        chunk = stream.read(size - len(parts))
        if not chunk:
            raise EOFError("compute pipe closed")
        parts.extend(chunk)
    return bytes(parts)


def read_frame(stream) -> ComputeEnvelope:
    size, = struct.unpack("!I", _read_exact(stream, 4))
    if not 0 < size <= MAX_FRAME_BYTES:
        raise ValueError("invalid compute frame length")
    value = parse_payload(_read_exact(stream, size))
    if set(value) != set(ComputeEnvelope.model_fields):
        raise ValueError("missing or unknown compute envelope field")
    return ComputeEnvelope.model_validate(value)


def encode_array(value) -> dict:
    import numpy as np

    array = np.asarray(value)
    name = array.dtype.name
    if name not in {"uint8", "float32", "float64", "bool"} or not 1 <= array.ndim <= 4:
        raise ValueError("unsupported compute array")
    if array.nbytes > MAX_ARRAY_BYTES or any(d > 4096 for d in array.shape):
        raise ValueError("compute array too large")
    if name.startswith("float") and not np.isfinite(array).all():
        raise ValueError("nonfinite compute array")
    array = np.ascontiguousarray(array, dtype=array.dtype.newbyteorder("<"))
    return {"dtype": name, "shape": list(array.shape), "data": base64.b64encode(array.tobytes()).decode("ascii")}


def decode_array(value, *, dtype=None, shape=None):
    import numpy as np

    if not isinstance(value, dict) or set(value) != {"dtype", "shape", "data"}:
        raise ValueError("invalid compute array fields")
    name, dims, data = value["dtype"], value["shape"], value["data"]
    if name not in {"uint8", "float32", "float64", "bool"} or dtype is not None and name != dtype:
        raise ValueError("compute dtype mismatch")
    if (not isinstance(dims, list) or not 1 <= len(dims) <= 4
            or any(type(d) is not int or not 0 <= d <= 4096 for d in dims)
            or shape is not None and tuple(dims) != tuple(shape)):
        raise ValueError("compute shape mismatch")
    dt = np.dtype(name).newbyteorder("<")
    size = math.prod(dims) * dt.itemsize
    if size > MAX_ARRAY_BYTES or not isinstance(data, str) or len(data) > (MAX_ARRAY_BYTES + 2) // 3 * 4:
        raise ValueError("compute array too large")
    raw = base64.b64decode(data, validate=True)
    if len(raw) != size or name == "bool" and any(byte > 1 for byte in raw):
        raise ValueError("compute byte count or boolean mismatch")
    array = np.frombuffer(raw, dtype=dt).reshape(dims).copy()
    if name.startswith("float") and not np.isfinite(array).all():
        raise ValueError("nonfinite compute array")
    return array
