"""Collect paired ColorBlob positions for offline tabletop policy fitting.

在独立仿真场景中配对采集 ColorBlob 位置与裁判位置，供离线策略拟合使用。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path

import numpy as np

from embodied.cli import _build_sim, _wrap_perception
from embodied.contracts.object_state import object_available


def collect_pairs(seed: int, scenes: int) -> dict:
    sim = _wrap_perception(_build_sim(seed, in_process=False), "color")
    samples = []
    try:
        asset_digest = sim.stop_controller.profile.asset_digest
        robot_id = sim.spec().embodiment_id
        for index in range(scenes):
            sim.reset(randomize=True)
            truth = sim.read_truth()
            observed = sim.read()
            sample = {"index": index, "truth": list(truth.objects["obj_cube"].pos)}
            if object_available(observed, "obj_cube"):
                pose = observed.objects["obj_cube"]
                sample.update(observed=list(pose.pos), quaternion=list(pose.quat))
            samples.append(sample)
    finally:
        sim.close()
    errors = [np.subtract(row["observed"], row["truth"]) for row in samples if "observed" in row]
    if len(errors) < 8:
        raise ValueError("fewer than eight paired ColorBlob observations")
    return {
        "schema": "embodied.pick-color-calibration/v1", "robot_id": robot_id, "asset_digest": asset_digest,
        "object": "obj_cube", "provider": "color", "seed": seed, "samples": samples,
        "available": len(errors), "total": scenes, "bias_m": np.mean(errors, axis=0).tolist(),
        "median_l2_m": float(np.median(np.linalg.norm(errors, axis=1))),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=81471)
    parser.add_argument("--scenes", type=int, default=64)
    parser.add_argument("--out-dir", type=Path, default=Path("outputs/calibration"))
    args = parser.parse_args()
    if args.scenes < 8:
        parser.error("at least eight scenes are needed for local error sampling")
    report = collect_pairs(args.seed, args.scenes)
    data = (json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    output = args.out_dir / f"pick-color-{digest}.json"
    if output.exists():
        if output.read_bytes() != data:
            raise ValueError("existing calibration file has different content")
    else:
        with tempfile.NamedTemporaryFile(dir=args.out_dir, suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
        try:
            temporary.write_bytes(data)
            os.replace(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
    print(json.dumps({"calibration": str(output), "sha256": digest, "available": report["available"],
                      "total": args.scenes, "median_l2_m": report["median_l2_m"]}, indent=2))


if __name__ == "__main__":
    main()
