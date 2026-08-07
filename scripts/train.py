#!/usr/bin/env python
"""One-command ACT training over a converted dataset (roadmap M2, docs/decisions.md D014).

    uv run --group learn python scripts/train.py --dataset outputs/lerobot/pick-v1

wraps ``lerobot-train`` with the flags that matter for this project: local dataset
root (no hub), hub push off, wandb off (lerobot default), dataloader workers 0
(Windows-safe), and a state-only ACT sized for CPU training. Checkpoints land in
``outputs/train/<dataset-name>/checkpoints/last/pretrained_model`` — the input
``scripts/export_onnx.py`` expects. Anything beyond these knobs: call lerobot-train
directly; this wrapper deliberately stays thin so lerobot upgrades stay cheap.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dataset", required=True, help="LeRobotDataset root produced by `embodied convert`")
    p.add_argument("--out", default="", help="training output dir (default outputs/train/<dataset name>)")
    p.add_argument("--steps", type=int, default=20_000)
    p.add_argument("--batch-size", type=int, default=32)
    p.add_argument("--chunk-size", type=int, default=50, help="ACT action chunk (frames @ dataset fps)")
    p.add_argument("--dim-model", type=int, default=256, help="transformer width (256 trains on CPU)")
    p.add_argument("--device", default="", help="cpu|cuda|mps (default: lerobot auto-detect)")
    p.add_argument("--lr", type=float, default=0.0,
                   help="override ACT preset lr (1e-5, tuned for image ACT; state-only trains fine at 1e-4)")
    p.add_argument("--seed", type=int, default=1000)
    p.add_argument("--log-freq", type=int, default=100)
    p.add_argument("--resume", action="store_true", help="resume from <out>/checkpoints/last")
    args = p.parse_args()

    root = Path(args.dataset)
    info_path = root / "meta" / "info.json"
    if not info_path.is_file():
        p.error(f"{root} is not a LeRobotDataset root (missing meta/info.json); run `embodied convert` first")
    fps = json.loads(info_path.read_text(encoding="utf-8")).get("fps")
    out = Path(args.out) if args.out else Path("outputs/train") / root.name

    cmd = [
        sys.executable, str(Path(__file__).with_name("_train_shim.py")),
        f"--dataset.repo_id=local/{root.name}",
        f"--dataset.root={root}",
        "--policy.type=act",
        "--policy.push_to_hub=false",
        f"--policy.chunk_size={args.chunk_size}",
        f"--policy.n_action_steps={args.chunk_size}",
        f"--policy.dim_model={args.dim_model}",
        f"--output_dir={out}",
        f"--steps={args.steps}",
        f"--batch_size={args.batch_size}",
        f"--seed={args.seed}",
        f"--log_freq={args.log_freq}",
        f"--save_freq={args.steps}",  # final checkpoint only; intermediate ones add no value at this scale
        "--num_workers=0",  # Windows: spawn-based dataloader workers cost more than they give here
    ]
    if args.device:
        cmd.append(f"--policy.device={args.device}")
    if args.lr > 0:
        cmd.append(f"--policy.optimizer_lr={args.lr}")
    if args.resume:
        cmd.append("--resume=true")
        cmd.append(f"--config_path={out / 'checkpoints' / 'last' / 'pretrained_model' / 'train_config.json'}")

    print(f"[train] dataset={root} (fps={fps}) -> {out}")
    print("[train] " + " ".join(cmd[2:]))
    code = subprocess.run(cmd).returncode
    if code == 0:
        print(f"[train] done -> {out / 'checkpoints' / 'last' / 'pretrained_model'}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
