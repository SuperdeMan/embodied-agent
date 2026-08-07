"""PolicyProvider family. ONNX tests auto-skip without the policy/learn groups
(CI installs neither); locally they MUST run."""

from __future__ import annotations

import json

import numpy as np
import pytest

from embodied.providers.policy import (
    INPUT_ENV,
    INPUT_STATE,
    OUTPUT_CHUNK,
    POLICY_ONNX_SCHEMA,
    MockChunkPolicy,
    OnnxPolicy,
    PolicyMeta,
    build_policy_provider,
)

META = PolicyMeta(chunk_size=4, control_hz=50.0, state_dim=6, env_dim=7, action_dim=6)


def test_mock_chunk_policy_reshapes_and_counts():
    p = MockChunkPolicy(META, lambda s, e: np.tile(s, (4, 1)))
    p.reset()
    out = p.act(np.arange(6, dtype=np.float32), np.zeros(7, dtype=np.float32))
    assert out.shape == (4, 6) and out.dtype == np.float32
    assert p.resets == 1 and p.calls == 1
    assert np.allclose(out[3], np.arange(6))


# -- ONNX path ---------------------------------------------------------------------

ort = pytest.importorskip("onnxruntime", reason="policy group not installed (uv sync --group policy)")
torch = pytest.importorskip("torch", reason="learn group not installed (uv sync --group learn)")
onnx = pytest.importorskip("onnx", reason="learn group not installed (uv sync --group learn)")


class Tiny(torch.nn.Module):
    """chunk[k] = state + k + env[0]; checkable in closed form."""

    def forward(self, observation_state, environment_state):
        rows = [observation_state + float(k) for k in range(4)]
        return torch.stack(rows, dim=1) + environment_state[:, :1].unsqueeze(1)


def _export(path, metadata: dict[str, str] | None) -> None:
    torch.onnx.export(
        Tiny().eval(),
        (torch.zeros(1, 6), torch.zeros(1, 7)),
        str(path),
        input_names=[INPUT_STATE, INPUT_ENV],
        output_names=[OUTPUT_CHUNK],
        dynamic_axes={INPUT_STATE: {0: "batch"}, INPUT_ENV: {0: "batch"}, OUTPUT_CHUNK: {0: "batch"}},
        opset_version=17,
        dynamo=False,
    )
    if metadata is not None:
        m = onnx.load(str(path))
        del m.metadata_props[:]
        for k, v in metadata.items():
            m.metadata_props.add(key=k, value=v)
        onnx.save(m, str(path))


@pytest.fixture(scope="module")
def artifact(tmp_path_factory):
    path = tmp_path_factory.mktemp("policy") / "tiny.onnx"
    _export(path, {
        "embodied.schema": POLICY_ONNX_SCHEMA,
        "chunk_size": "4", "control_hz": "50.0", "state_dim": "6", "env_dim": "7",
        "action_dim": "6", "task": "skill.manip.pick",
        "state_names": json.dumps(["a", "b", "c", "d", "e", "gripper"]),
        "source": "tests",
    })
    return path


def test_onnx_policy_parses_metadata(artifact):
    p = OnnxPolicy(artifact)
    assert p.meta.chunk_size == 4 and p.meta.control_hz == 50.0
    assert p.meta.state_dim == 6 and p.meta.env_dim == 7 and p.meta.action_dim == 6
    assert p.meta.task == "skill.manip.pick"
    assert p.meta.state_names == ("a", "b", "c", "d", "e", "gripper")


def test_onnx_policy_act_values(artifact):
    p = build_policy_provider(artifact)
    state = np.arange(6, dtype=np.float32)
    env = np.full(7, 10.0, dtype=np.float32)
    out = p.act(state, env)
    assert out.shape == (4, 6) and out.dtype == np.float32
    for k in range(4):
        assert np.allclose(out[k], state + k + 10.0), k


def test_onnx_without_schema_rejected(tmp_path):
    path = tmp_path / "bare.onnx"
    _export(path, None)
    with pytest.raises(ValueError, match="not an embodied policy artifact"):
        OnnxPolicy(path)
