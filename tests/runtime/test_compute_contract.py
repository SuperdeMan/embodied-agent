"""Bounded numeric wire data, never Python object deserialization. 有界数值线格式，不反序列化 Python 对象。"""

import base64
import io
import json
import struct

import numpy as np
import pytest

from embodied.contracts.compute import (
    MAX_FRAME_BYTES,
    ComputeEnvelope,
    decode_array,
    encode_array,
    encode_frame,
    parse_payload,
    read_frame,
)


@pytest.mark.parametrize("dtype", ["uint8", "float32", "float64", "bool"])
def test_array_roundtrip_is_owned_and_finite(dtype):
    array = np.arange(6).reshape(2, 3).astype(dtype)
    decoded = decode_array(encode_array(array), dtype=dtype, shape=(2, 3))
    np.testing.assert_array_equal(decoded, array)
    assert decoded.flags.owndata


@pytest.mark.parametrize("change", [
    {"dtype": "object"}, {"shape": [True]}, {"shape": [-1]}, {"shape": [4097]}, {"shape": []},
    {"shape": [4096, 4096, 4096]}, {"data": "garbage"}, {"data": ""}, {"extra": "forbidden"},
])
def test_malformed_array_rejected(change):
    data = encode_array(np.zeros(3, dtype=np.float32))
    data.update(change)
    with pytest.raises(ValueError):
        decode_array(data)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_arrays_rejected_on_both_sides(value):
    array = np.array([value], dtype=np.float32)
    with pytest.raises(ValueError):
        encode_array(array)
    with pytest.raises(ValueError):
        decode_array({"dtype": "float32", "shape": [1], "data": base64.b64encode(array.tobytes()).decode()})


def test_noncanonical_boolean_and_dtype_shape_mismatch_rejected():
    with pytest.raises(ValueError):
        decode_array({"dtype": "bool", "shape": [1], "data": base64.b64encode(b"\x02").decode()})
    data = encode_array(np.zeros((2, 3), dtype=np.float32))
    with pytest.raises(ValueError):
        decode_array(data, dtype="float64")
    with pytest.raises(ValueError):
        decode_array(data, shape=(3, 2))


def test_frame_roundtrip_and_size_limit():
    envelope = ComputeEnvelope(boot_id="boot", request_id="1", kind="call", payload_json="{}")
    assert read_frame(io.BytesIO(encode_frame(envelope))) == envelope
    with pytest.raises(ValueError):
        read_frame(io.BytesIO(struct.pack("!I", MAX_FRAME_BYTES + 1)))
    with pytest.raises(EOFError):
        read_frame(io.BytesIO(struct.pack("!I", 10) + b"{}"))


@pytest.mark.parametrize("field,value", [("api_version", "unknown"), ("kind", "motor_write"),
                                         ("boot_id", ""), ("request_id", 1), ("extra", "forbidden")])
def test_invalid_envelope_rejected(field, value):
    payload = ComputeEnvelope(boot_id="boot", request_id="1", kind="call", payload_json="{}").model_dump()
    payload[field] = value
    raw = json.dumps(payload).encode()
    with pytest.raises(ValueError):
        read_frame(io.BytesIO(struct.pack("!I", len(raw)) + raw))


@pytest.mark.parametrize("raw", ['{"x":NaN}', '{"x":1e999}', '{"x":1,"x":2}', '[1,2]', '{"a":{"x":1,"x":2}}'])
def test_ambiguous_json_rejected(raw):
    with pytest.raises(ValueError):
        parse_payload(raw)


def test_json_mapping_matches_proto_declaration():
    pytest.importorskip("grpc")
    from embodied.runtime.rpcgen import import_control
    import_control()
    from embodiedrpc.compute.v1 import compute_pb2
    from google.protobuf.json_format import MessageToDict, ParseDict

    payload = ComputeEnvelope(boot_id="boot", request_id="1", kind="call", payload_json="{}").model_dump()
    message = ParseDict(payload, compute_pb2.ComputeEnvelope())
    assert MessageToDict(message, preserving_proto_field_name=True) == payload
    array = encode_array(np.zeros((2, 3), dtype=np.float32))
    assert MessageToDict(ParseDict(array, compute_pb2.ComputeArray()), preserving_proto_field_name=True) == array
