"""Run the scoped P1 fault matrix with commit-bound evidence.

运行限定范围的 P1 故障矩阵并保存绑定提交的证据。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=ROOT / "outputs" / "p1-faults")
    args = parser.parse_args()
    matrix_path = ROOT / "eval/p1-fault-matrix.json"
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    clean = not subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT).strip()
    output = args.out / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + commit[:7])
    output.mkdir(parents=True, exist_ok=False)
    files = [name for group in matrix["groups"] for name in group["tests"]]
    assert len(files) == len(set(files)) and all((ROOT / name).is_file() for name in files)
    xml = output / "results.xml"
    command = [sys.executable, "-m", "pytest", *files, "-q", "-o", "addopts=", "--import-mode=importlib",
               "--junitxml=" + str(xml.resolve())]
    started = time.monotonic()
    with (output / "pytest.log").open("w", encoding="utf-8") as log:
        completed = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    cases = ET.parse(xml).getroot().findall(".//testcase") if xml.exists() else []
    groups = []
    for group in matrix["groups"]:
        names = {Path(name).stem for name in group["tests"]}
        selected = [case for case in cases if case.get("classname", "").rsplit(".", 1)[-1] in names]
        durations = sorted(float(case.get("time", "0")) for case in selected)
        failed = sum(case.find("failure") is not None or case.find("error") is not None for case in selected)
        skipped = sum(case.find("skipped") is not None for case in selected)
        missing = sorted(names - {case.get("classname", "").rsplit(".", 1)[-1] for case in selected})
        groups.append({**group, "total": len(selected), "passed": len(selected) - failed - skipped,
                       "failed": failed, "skipped": skipped, "missing_modules": missing,
                       # 这是测试执行时间，不是物理停止延迟。 / These are test durations, not physical stop latencies.
                       "test_duration_s": {"p95": durations[math.ceil(len(durations) * .95) - 1] if durations else None,
                                           "max": max(durations, default=None)}})
    passed = completed.returncode == 0 and all(g["total"] and not (g["failed"] or g["skipped"] or g["missing_modules"]) for g in groups)
    result = {"schema": matrix["schema"], "commit": commit, "working_tree_clean": clean,
              "matrix_sha256": hashlib.sha256(matrix_path.read_bytes()).hexdigest(),
              "reference_checks_passed": passed, "p1_complete": False, "groups": groups,
              "open_gates": matrix["open_gates"], "wall_duration_s": time.monotonic() - started,
              "returncode": completed.returncode, "command": command,
              "junit_sha256": hashlib.sha256(xml.read_bytes()).hexdigest() if xml.exists() else None}
    (output / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(output / "report.json"), "reference_checks_passed": passed,
                      "total": len(cases), "p1_complete": False}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
