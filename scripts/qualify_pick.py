"""Fit a reference pick model and qualify it in the isolated simulator in one command.

用一条命令拟合抓取模型，在隔离仿真中与同配置脚本比较并保存验收回执。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from embodied.data_engine.evaluation import EvalTask

ROOT = Path(__file__).resolve().parents[1]
TASK = Path("eval/tasks/tabletop_pick_place_v1.yaml")
PLACE = Path("outputs/policies/place-v1.onnx")


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def build_json(script: str, *args: str) -> dict:
    data = subprocess.check_output([sys.executable, str(ROOT / "scripts" / script), *args], cwd=ROOT)
    return json.loads(data)


def evaluate(directory: Path, perception: str, *policies: str) -> dict:
    directory.mkdir()
    args = [sys.executable, "-m", "embodied.cli", "sim", "--task", TASK.as_posix(),
            "--perception", perception, *policies]
    with (directory / "console.log").open("wb") as output:
        proc = subprocess.run(args, cwd=ROOT, env={**os.environ, "EVAL_OUT_DIR": str(directory.resolve())},
                              stdout=output, stderr=subprocess.STDOUT, check=False)
    files = list(directory.glob("*.json"))
    if len(files) != 1:
        raise RuntimeError(f"expected one evaluation report in {directory}, found {len(files)}")
    raw = json.loads(files[0].read_bytes())
    return {"report": files[0].relative_to(ROOT).as_posix(), "report_sha256": digest(files[0]),
            "commit": raw["commit"], "task": raw["task"]["id"], "schema": raw["schema"],
            "complete": raw["complete"], "planned_total": raw["planned_total"], "total": raw["total"],
            "wins": raw["wins"], "exit_code": proc.returncode}


def matched_reference(task: EvalTask, scripted: dict, learned: dict) -> bool:
    """Both runs must finish every reference case. 两组都必须完成全部原版局次。"""
    total = len(task.seeds) * task.per_seed
    return (all(run["schema"] == "eval.result/v1" and run["complete"] and run["exit_code"] == 0
                and run["task"] == task.task_id and run["total"] == run["planned_total"] == total
                for run in (scripted, learned))
            and re.fullmatch(r"[0-9a-f]{7,40}", scripted["commit"]) is not None
            and scripted["commit"] == learned["commit"]
            and scripted["wins"] == learned["wins"] == total)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True, help="local LeRobot v3 pick-only demonstration dataset")
    parser.add_argument("--perception", choices=("truth", "color"), required=True)
    args = parser.parse_args()
    dataset = args.dataset.resolve(strict=True)
    reference = json.loads((ROOT / "eval/upgrade-20261030.json").read_bytes())
    place_digest = digest(ROOT / PLACE)
    if place_digest != reference["files"][PLACE.as_posix()]:
        parser.error("place-v1 checksum differs from the reference")
    task_path = ROOT / TASK
    task_digest = digest(task_path)
    reference_task = json.loads((ROOT / "eval/learned-pick-residual-20261129.json").read_bytes())["task_sets"]["reference"]
    task_lf_digest = hashlib.sha256(task_path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    if task_lf_digest != reference_task["lf_sha256"]:
        parser.error("task checksum differs from the fixed 30-case reference")
    task = EvalTask.load(task_path)
    directory = ROOT / "outputs/pick-qualification" / uuid4().hex
    directory.mkdir(parents=True)
    print(f"Reports: {directory}", flush=True)
    calibration = build_json("calibrate_pick_observation.py") if args.perception == "color" else None
    options = ["--color-calibration", calibration["calibration"]] if calibration else []
    model = build_json("fit_pick_residual.py", "--dataset", str(dataset), *options)
    pick = ROOT / model["artifact"]
    if digest(pick) != model["onnx_sha256"]:
        raise ValueError("fitted artifact checksum differs from its manifest")
    sensor = "color" if calibration else "off"
    control = evaluate(directory / "scripted", sensor)
    candidate = evaluate(directory / "learned", sensor, "--pick-policy", str(pick), "--place-policy", str(ROOT / PLACE))
    passed = (matched_reference(task, control, candidate) and digest(task_path) == task_digest
              and digest(pick) == model["onnx_sha256"] and digest(ROOT / PLACE) == place_digest)
    result = {"schema": "embodied.pick-qualification/v1", "qualified": passed, "perception": args.perception,
              "task_sha256": task_digest, "task_lf_sha256": task_lf_digest, "calibration": calibration,
              "pick": model, "place_sha256": place_digest,
              "scripted": control, "learned": candidate}
    target = directory / "qualification.json"
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{control['wins']}/{control['total']} scripted, {candidate['wins']}/{candidate['total']} learned, "
          f"qualified={passed}, {target}", flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

