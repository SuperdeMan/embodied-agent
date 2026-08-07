#!/usr/bin/env python
"""Export a lerobot ACT checkpoint to the single-file ONNX artifact OnnxPolicy runs.

    uv run --group learn python scripts/export_onnx.py \
        --checkpoint outputs/train/act-pick-v1/checkpoints/last/pretrained_model \
        --out outputs/policies/pick-v1.onnx --task skill.manip.pick

Bakes normalization INTO the graph so deployment feeds raw observations and gets
absolute joint targets: state is normalized with the preprocessor's MEAN_STD stats,
environment_state passes through raw (lerobot's ACT norm_map has no ENV entry ->
IDENTITY; asserted here so a lerobot behavior change fails loudly instead of
silently skewing actions), and the action chunk is unnormalized with the
postprocessor's stats (x*std+mean — lerobot's exact inverse form). The export is
verified against the torch policy before metadata is written.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

EPS = 1e-8  # lerobot normalizer epsilon (normalize divides by std+eps; inverse is x*std+mean)


def _load_norm_stats(checkpoint: Path) -> tuple[dict, dict]:
    """Return (state_stats, action_stats) as {mean, std} float32 arrays, after
    asserting the processor pipeline still matches the contract we bake in."""
    from safetensors.torch import load_file

    pre_cfg = json.loads((checkpoint / "policy_preprocessor.json").read_text(encoding="utf-8"))
    norm_steps = [s for s in pre_cfg["steps"] if s["registry_name"] == "normalizer_processor"]
    if len(norm_steps) != 1:
        raise RuntimeError(f"expected exactly one normalizer_processor step, got {len(norm_steps)}")
    cfg = norm_steps[0]["config"]
    norm_map = cfg.get("norm_map", {})
    if norm_map.get("STATE") != "MEAN_STD" or norm_map.get("ACTION") != "MEAN_STD":
        raise RuntimeError(f"unexpected norm_map {norm_map} — this exporter bakes MEAN_STD for STATE/ACTION")
    if "ENV" in norm_map and norm_map["ENV"] != "IDENTITY":
        raise RuntimeError(f"norm_map now normalizes ENV ({norm_map['ENV']}) — update the exporter to match")
    if abs(float(cfg.get("eps", EPS)) - EPS) > 1e-12:
        raise RuntimeError(f"normalizer eps changed to {cfg.get('eps')} — update EPS to match")

    stats = load_file(str(checkpoint / norm_steps[0]["state_file"]))

    def pick(prefix: str) -> dict:
        return {
            "mean": stats[f"{prefix}.mean"].numpy().astype(np.float32),
            "std": stats[f"{prefix}.std"].numpy().astype(np.float32),
        }

    return pick("observation.state"), pick("action")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--checkpoint", required=True, help=".../checkpoints/last/pretrained_model directory")
    p.add_argument("--out", required=True, help="output .onnx path")
    p.add_argument("--task", default="", help="skill name this policy implements (e.g. skill.manip.pick)")
    p.add_argument("--control-hz", type=float, default=50.0, help="dataset fps the policy was trained at")
    args = p.parse_args()

    import onnx
    import torch
    from lerobot.policies.act.modeling_act import ACTPolicy
    from lerobot.utils.constants import OBS_ENV_STATE, OBS_STATE

    from embodied.providers.policy import (
        INPUT_ENV,
        INPUT_STATE,
        OUTPUT_CHUNK,
        POLICY_ONNX_SCHEMA,
    )

    checkpoint = Path(args.checkpoint)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    policy = ACTPolicy.from_pretrained(str(checkpoint))
    policy.eval()
    cfg = policy.config
    state_dim = cfg.input_features[OBS_STATE].shape[0]
    env_dim = cfg.input_features[OBS_ENV_STATE].shape[0]
    action_dim = cfg.output_features["action"].shape[0]
    state_stats, action_stats = _load_norm_stats(checkpoint)

    class ChunkExport(torch.nn.Module):
        """raw (state, env) -> unnormalized action chunk; eval mode => VAE latent zeros."""

        def __init__(self, model: torch.nn.Module) -> None:
            super().__init__()
            self.model = model
            self.register_buffer("s_mean", torch.from_numpy(state_stats["mean"]))
            self.register_buffer("s_std", torch.from_numpy(state_stats["std"]))
            self.register_buffer("a_mean", torch.from_numpy(action_stats["mean"]))
            self.register_buffer("a_std", torch.from_numpy(action_stats["std"]))

        def forward(self, observation_state: torch.Tensor, environment_state: torch.Tensor) -> torch.Tensor:
            s = (observation_state - self.s_mean) / (self.s_std + EPS)
            chunk = self.model({OBS_STATE: s, OBS_ENV_STATE: environment_state})[0]
            return chunk * self.a_std + self.a_mean

    wrapper = ChunkExport(policy.model).eval()
    sample = (torch.zeros(1, state_dim), torch.zeros(1, env_dim))
    with torch.no_grad():
        expected = wrapper(*sample).numpy()

    torch.onnx.export(
        wrapper,
        sample,
        str(out_path),
        input_names=[INPUT_STATE, INPUT_ENV],
        output_names=[OUTPUT_CHUNK],
        dynamic_axes={INPUT_STATE: {0: "batch"}, INPUT_ENV: {0: "batch"}, OUTPUT_CHUNK: {0: "batch"}},
        opset_version=17,
        dynamo=False,
    )

    # Parity gate BEFORE stamping metadata: a silently-wrong graph must not ship.
    import onnxruntime as ort

    sess = ort.InferenceSession(str(out_path), providers=["CPUExecutionProvider"])
    (got,) = sess.run([OUTPUT_CHUNK], {INPUT_STATE: sample[0].numpy(), INPUT_ENV: sample[1].numpy()})
    err = float(np.max(np.abs(got - expected)))
    if err > 1e-4:
        raise RuntimeError(f"ONNX/torch mismatch: max abs err {err:.2e} > 1e-4; not writing artifact")

    model = onnx.load(str(out_path))
    meta = {
        "embodied.schema": POLICY_ONNX_SCHEMA,
        "chunk_size": str(cfg.chunk_size),
        "control_hz": str(args.control_hz),
        "state_dim": str(state_dim),
        "env_dim": str(env_dim),
        "action_dim": str(action_dim),
        "task": args.task,
        "source": str(checkpoint),
    }
    del model.metadata_props[:]
    for k, v in meta.items():
        model.metadata_props.add(key=k, value=v)
    onnx.save(model, str(out_path))
    print(f"[export] {checkpoint} -> {out_path} (chunk={cfg.chunk_size}, parity err {err:.2e})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
