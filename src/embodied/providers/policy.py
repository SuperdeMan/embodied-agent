"""PolicyProvider family: learned-skill inference (docs/architecture.md §5/§7, D014).

Training happens in the ``learn`` dependency group (torch); deployment runs an
exported ONNX artifact in the ``policy`` group (onnxruntime only — no torch at
runtime, ever). The artifact is a single .onnx file whose embedded metadata carries
its full IO contract (chunk size, control rate, dims, feature layout);
``scripts/export_onnx.py`` produces it from a lerobot ACT checkpoint with the
normalization baked into the graph, so callers feed raw observations and receive
absolute joint targets.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

POLICY_ONNX_SCHEMA = "embodied.policy.onnx/v1"

# Graph IO names, shared with scripts/export_onnx.py (dots are not valid ONNX names,
# hence underscores; the dataset-side names remain observation.state etc.).
INPUT_STATE = "observation_state"
INPUT_ENV = "environment_state"
OUTPUT_CHUNK = "action_chunk"


@dataclass(frozen=True)
class PolicyMeta:
    """IO contract of a policy artifact (parsed from ONNX embedded metadata)."""

    chunk_size: int
    control_hz: float
    state_dim: int
    env_dim: int
    action_dim: int
    task: str = ""
    state_names: tuple[str, ...] = ()
    env_names: tuple[str, ...] = ()
    source: str = ""  # provenance: checkpoint path / dataset id


class BasePolicyProvider(ABC):
    """A learned policy: observations in, an absolute-target action chunk out."""

    @property
    @abstractmethod
    def meta(self) -> PolicyMeta: ...

    def reset(self) -> None:
        """Clear per-rollout state (action queues, ensembling). Default: stateless."""
        return None

    @abstractmethod
    def act(self, state: np.ndarray, env: np.ndarray) -> np.ndarray:
        """``(state_dim,)``, ``(env_dim,)`` -> ``(chunk_size, action_dim)`` targets."""


class OnnxPolicy(BasePolicyProvider):
    """ONNX Runtime inference over an exported policy artifact (CPU by default)."""

    def __init__(self, path: Path | str) -> None:
        try:
            import onnxruntime as ort
        except ImportError as e:  # pragma: no cover - exercised only without the policy group
            raise RuntimeError(
                "onnxruntime is required for policy inference: uv sync --group policy (D014)"
            ) from e
        self.path = Path(path)
        self.session = ort.InferenceSession(str(self.path), providers=["CPUExecutionProvider"])
        md = dict(self.session.get_modelmeta().custom_metadata_map)
        if md.get("embodied.schema") != POLICY_ONNX_SCHEMA:
            raise ValueError(
                f"{self.path}: not an embodied policy artifact "
                f"(schema={md.get('embodied.schema')!r}; export with scripts/export_onnx.py)"
            )
        self._meta = PolicyMeta(
            chunk_size=int(md["chunk_size"]),
            control_hz=float(md["control_hz"]),
            state_dim=int(md["state_dim"]),
            env_dim=int(md["env_dim"]),
            action_dim=int(md["action_dim"]),
            task=md.get("task", ""),
            state_names=tuple(json.loads(md.get("state_names", "[]"))),
            env_names=tuple(json.loads(md.get("env_names", "[]"))),
            source=md.get("source", ""),
        )

    @property
    def meta(self) -> PolicyMeta:
        return self._meta

    def act(self, state: np.ndarray, env: np.ndarray) -> np.ndarray:
        m = self._meta
        state = np.asarray(state, dtype=np.float32).reshape(m.state_dim)
        env = np.asarray(env, dtype=np.float32).reshape(m.env_dim)
        (chunk,) = self.session.run([OUTPUT_CHUNK], {INPUT_STATE: state[None], INPUT_ENV: env[None]})
        out = np.asarray(chunk, dtype=np.float32)[0]
        if out.shape != (m.chunk_size, m.action_dim):
            raise ValueError(f"{self.path}: model returned {out.shape}, metadata says {(m.chunk_size, m.action_dim)}")
        return out


class MockChunkPolicy(BasePolicyProvider):
    """Deterministic test double: ``fn(state, env) -> chunk`` with a declared meta."""

    def __init__(self, meta: PolicyMeta, fn: Callable[[np.ndarray, np.ndarray], np.ndarray]) -> None:
        self._meta = meta
        self._fn = fn
        self.resets = 0
        self.calls = 0

    @property
    def meta(self) -> PolicyMeta:
        return self._meta

    def reset(self) -> None:
        self.resets += 1

    def act(self, state: np.ndarray, env: np.ndarray) -> np.ndarray:
        self.calls += 1
        out = np.asarray(self._fn(np.asarray(state), np.asarray(env)), dtype=np.float32)
        return out.reshape(self._meta.chunk_size, self._meta.action_dim)


def build_policy_provider(path: Path | str) -> BasePolicyProvider:
    """Factory (mirrors build_provider/build_asr_provider): one artifact kind today."""
    return OnnxPolicy(path)
