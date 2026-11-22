"""Offline physical-control process-isolation probe for Linux candidate containers (D034).

Linux 候选容器离线物理控制进程隔离探针（D034）：验证三进程分离、数值一致、故障强杀回收、无重放与重启保留闩锁。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import sysconfig
import threading
import time
from pathlib import Path
from uuid import uuid4

import numpy as np

from embodied.control.drivers.mujoco_sim import TabletopSim
from embodied.control.isolated_sim import IsolatedTabletopSim
from embodied.runtime.control_process import ControlProcess, PhysicalControlUnavailable
from embodied.skills.registry import SkillRegistry
from embodied.skills.scripted.manip import register_sim_skills

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        raise ValueError("probe output already exists")
    folder = args.out.parent / ("control-probe-" + uuid4().hex)
    folder.mkdir(parents=True)

    bootstrap = (
        "import sys,site,runpy; p=sys.argv.pop(1); sys.path.insert(0,p); site.addsitedir(p); "
        "runpy.run_path(sys.argv.pop(1),run_name='__main__')"
    )
    stub_cmd = [
        getattr(sys, "_base_executable", sys.executable),
        "-I",
        "-u",
        "-c",
        bootstrap,
        sysconfig.get_path("purelib"),
        str(ROOT / "tests/runtime/control_worker_fixture.py"),
        "--mode",
        "stub",
    ]

    # 1. 验证全部故障类型（含持 GIL 原生挂起、崩溃、abort、错代次、超长帧）在有界时间回收且零重放。
    # 1. Verify all fault modes (incl. GIL hang, crash, abort, wrong boot, oversize) reap in bounded time with zero replay.
    faults = []
    for fault in ("hang", "gil_hang", "crash", "abort", "wrong_boot", "oversize"):
        trace = folder / f"{fault}.txt"
        worker = ControlProcess(
            {"trace": str(trace)}, timeout_s=0.15, startup_timeout_s=10.0, _command=stub_cmd,
        )
        worker.start()
        boot, pid = worker.boot_id, worker.last_pid
        started = time.monotonic()
        try:
            try:
                worker.call("step", {"fault": fault})
                raise AssertionError(f"fault {fault} unexpectedly succeeded")
            except PhysicalControlUnavailable:
                pass
            elapsed = time.monotonic() - started
            assert worker.state == "FAILED" and worker.reaped and elapsed < 3.0
            assert trace.read_text(encoding="utf-8").splitlines() == ["step:"]
            worker.restart()
            assert worker.boot_id != boot and worker.last_pid != pid
            assert trace.read_text(encoding="utf-8").splitlines() == ["step:"]
            assert worker.call("read", {})["op"] == "read"
            faults.append({
                "fault": fault,
                "old_pid": pid,
                "seconds": elapsed,
                "reaped": True,
                "request_replayed": False,
            })
        finally:
            worker.close()
        assert worker.reaped

    # 2. 验证 urgent_estop 跨线程强杀持 GIL 挂起子进程树。
    # 2. Verify urgent_estop forcibly kills and reaps a GIL-hung worker tree cross-thread.
    hung = ControlProcess({}, timeout_s=10.0, startup_timeout_s=10.0, cancel_grace_s=0.12, _command=stub_cmd)
    try:
        hung.start()
        entered = threading.Event()
        err: list[BaseException] = []

        def run_hung():
            entered.set()
            try:
                hung.call("step", {"fault": "gil_hang"})
            except BaseException as exc:
                err.append(exc)

        th = threading.Thread(target=run_hung)
        th.start()
        assert entered.wait(2.0)
        deadline = time.monotonic() + 2.0
        while not hung.busy and time.monotonic() < deadline:
            time.sleep(0.005)
        t0 = time.monotonic()
        soft = hung.urgent_estop("linux_urgent_kill")
        urgent_s = time.monotonic() - t0
        th.join(timeout=2.0)
        assert soft is False and not th.is_alive() and hung.state == "FAILED" and hung.reaped and urgent_s < 2.0
    finally:
        hung.close()

    # 3. 真实 MuJoCo 三进程隔离与数值对照（父进程 / 物理控制子进程 / 渲染子进程 PID 互异）。
    # 3. Real MuJoCo 3-process separation and numerical parity (distinct parent / physics / render PIDs).
    iso = IsolatedTabletopSim(seed=0)
    ref = TabletopSim(seed=0, isolated_render=False)
    try:
        o_iso, o_ref = iso.read(), ref.read()
        np.testing.assert_allclose(o_iso.qpos, o_ref.qpos, rtol=0, atol=1e-12)
        np.testing.assert_allclose(o_iso.ee_pose.pos, o_ref.ee_pose.pos, rtol=0, atol=1e-12)
        jpeg = iso.render_jpeg("side")
        assert jpeg[:2] == b"\xff\xd8"
        control_pid = iso.control_pid
        render_pid = iso._render_process.last_pid if iso._render_process is not None else None
        assert len({os.getpid(), control_pid, render_pid}) == 3

        registry = SkillRegistry(robot_id=iso.spec().embodiment_id)
        register_sim_skills(registry, iso)
        try:
            async def _pick_place():
                r1 = await registry.invoke("skill.manip.pick", {"object": "obj_cube"})
                r2 = await registry.invoke("skill.manip.place", {"region": "bin_region"})
                return r1, r2

            r1, r2 = asyncio.run(_pick_place())
            assert r1.ok and r2.ok
        finally:
            registry.close()
    finally:
        iso.close()
        ref.close()
    assert iso._control_process.reaped and iso._control_process.shutdown_acknowledged and iso._control_process.last_exitcode == 0

    report = {
        "passed": True,
        "parent_pid": os.getpid(),
        "control_pid": control_pid,
        "render_pid": render_pid,
        "three_distinct_pids": True,
        "numerical_parity": True,
        "pick_place_ok": True,
        "urgent_estop_kill_s": urgent_s,
        "graceful_ack": iso._control_process.shutdown_acknowledged,
        "exitcode": iso._control_process.last_exitcode,
        "faults": faults,
    }
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
