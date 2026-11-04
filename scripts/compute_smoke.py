"""Offline process-lifecycle probe for the candidate container. 候选容器的离线进程生命周期探针。"""

from __future__ import annotations

import argparse
import json
import os
import sys
import sysconfig
import time
from pathlib import Path
from uuid import uuid4

import numpy as np

from embodied.providers.perception import ColorBlobProvider, build_perception_provider
from embodied.runtime.compute import ComputeProcess, ComputeUnavailable

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        raise ValueError("probe output already exists")
    folder = args.out.parent / ("compute-probe-" + uuid4().hex)
    folder.mkdir(parents=True)
    bootstrap = "import sys,site,runpy;p=sys.argv.pop(1);sys.path.insert(0,p);site.addsitedir(p);runpy.run_path(sys.argv.pop(1),run_name='__main__')"
    command = [getattr(sys, "_base_executable", sys.executable), "-I", "-u", "-c", bootstrap,
               sysconfig.get_path("purelib"), str(ROOT / "tests/runtime/compute_fixture.py")]
    faults = []
    for fault in ("hang", "gil_hang", "crash", "abort", "wrong_boot", "oversize"):
        trace = folder / (fault + ".txt")
        worker = ComputeProcess({"trace": str(trace)}, timeout_s=0.1, startup_timeout_s=10, _command=command)
        worker.start()
        boot, pid = worker.boot_id, worker.last_pid
        started = time.monotonic()
        try:
            try:
                worker.call(fault, {})
                raise AssertionError("fault request unexpectedly succeeded")
            except ComputeUnavailable:
                pass
            elapsed = time.monotonic() - started
            assert worker.state == "FAILED" and worker.reaped and elapsed < 3
            assert trace.read_text().splitlines() == [fault]
            worker.restart()
            assert worker.boot_id != boot and trace.read_text().splitlines() == [fault]
            assert worker.call("echo", {"value": "fresh"})["value"] == "fresh"
            faults.append({"fault": fault, "old_pid": pid, "seconds": elapsed, "reaped": True, "request_replayed": False})
        finally:
            worker.close()
        assert worker.reaped
    rgb = np.zeros((64, 64, 3), dtype=np.uint8)
    rgb[10:40, 20:45] = [255, 0, 0]
    provider = build_perception_provider("color", isolated=True)
    try:
        actual = provider.detect(rgb, ["color:red"])[0]
        expected = ColorBlobProvider().detect(rgb, ["color:red"])[0]
        assert (actual.label, actual.score, actual.bbox) == (expected.label, expected.score, expected.bbox)
        np.testing.assert_array_equal(actual.mask, expected.mask)
        pid = provider.worker.last_pid
        assert pid != os.getpid()
    finally:
        provider.close()
    assert provider.worker.reaped
    report = {"passed": True, "faults": faults, "color_pid": pid, "color_reference_equal": True,
              "color_reaped": provider.worker.reaped, "graceful_ack": provider.worker.shutdown_acknowledged,
              "exitcode": provider.worker.last_exitcode}
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
