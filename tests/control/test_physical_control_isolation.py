"""Real MuJoCo isolated physical control integration, fault containment, and recovery tests (D034).

基于真实 MuJoCo 物理控制子进程的数值对照、停止对账、原生挂起/崩溃隔离与显式恢复集成测试（D034）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import sysconfig
import threading
import time
from pathlib import Path

import numpy as np
import pytest
from aiohttp.test_utils import TestClient, TestServer

pytest.importorskip("mujoco")

from embodied.cognition.perception import PerceivedSim, PerceptionPipeline  # noqa: E402
from embodied.cognition.world_state import WorldSnapshot, held_object  # noqa: E402
from embodied.contracts.effect import EffectRejected  # noqa: E402
from embodied.contracts.operation import OperationIdentity  # noqa: E402
from embodied.contracts.stopping import StopRejected  # noqa: E402
from embodied.control.actions import ActionRejected  # noqa: E402
from embodied.control.drivers.mujoco_sim import TabletopSim  # noqa: E402
from embodied.control.isolated_sim import IsolatedTabletopSim  # noqa: E402
from embodied.control.simassets import find_menagerie_model  # noqa: E402
from embodied.hri.server import AppContext, build_app  # noqa: E402
from embodied.providers.perception import build_perception_provider  # noqa: E402
from embodied.runtime.control_process import PhysicalControlUnavailable  # noqa: E402
from embodied.runtime.invocation import run_blocking  # noqa: E402
from embodied.runtime.operations import OperationLedger  # noqa: E402
from embodied.skills.manifest import SkillManifest, TerminationSpec  # noqa: E402
from embodied.skills.registry import SkillRegistry, SkillResult  # noqa: E402
from embodied.skills.scripted.manip import register_sim_skills  # noqa: E402

try:
    find_menagerie_model()
except FileNotFoundError:
    pytest.skip("menagerie assets not fetched", allow_module_level=True)

FIXTURE = Path(__file__).parents[1] / "runtime" / "control_worker_fixture.py"


def _real_fixture_command(fault_file: Path, trace_file: Path) -> list[str]:
    bootstrap = (
        "import sys,site,runpy; p=sys.argv.pop(1); sys.path.insert(0,p); site.addsitedir(p); "
        "runpy.run_path(sys.argv.pop(1),run_name='__main__')"
    )
    return [
        getattr(sys, "_base_executable", sys.executable),
        "-I",
        "-u",
        "-c",
        bootstrap,
        sysconfig.get_path("purelib"),
        str(FIXTURE),
        "--mode",
        "real",
        "--fault-file",
        str(fault_file),
        "--trace",
        str(trace_file),
    ]


def test_isolated_sim_three_process_separation_and_numerical_parity():
    iso = IsolatedTabletopSim(seed=0)
    ref = TabletopSim(seed=0, isolated_render=False)
    try:
        assert iso.control_pid is not None and iso.control_pid != os.getpid()
        assert not hasattr(iso, "model") and not hasattr(iso, "data")
        obs_iso, obs_ref = iso.read(), ref.read()
        np.testing.assert_allclose(obs_iso.qpos, obs_ref.qpos, rtol=0, atol=1e-12)
        np.testing.assert_allclose(obs_iso.qvel, obs_ref.qvel, rtol=0, atol=1e-12)
        np.testing.assert_allclose(obs_iso.ee_pose.pos, obs_ref.ee_pose.pos, rtol=0, atol=1e-12)
        np.testing.assert_allclose(obs_iso.objects["obj_cube"].pos, obs_ref.objects["obj_cube"].pos, rtol=0, atol=1e-12)

        ik_iso = iso.solve_reach(obs_iso.objects["obj_cube"].pos)
        ik_ref = ref.solve_reach(obs_ref.objects["obj_cube"].pos)
        np.testing.assert_allclose(ik_iso.qpos, ik_ref.qpos, rtol=0, atol=1e-10)
        assert abs(ik_iso.pos_err - ik_ref.pos_err) < 1e-10

        cam_iso = iso.camera_model("side", 320, 240)
        cam_ref = ref.camera_model("side", 320, 240)
        assert cam_iso == cam_ref

        rgb_iso, depth_iso = iso.render_rgbd("side", 160, 120)
        rgb_ref, depth_ref = ref.render_rgbd("side", 160, 120)
        np.testing.assert_array_equal(rgb_iso, rgb_ref)
        np.testing.assert_array_equal(depth_iso, depth_ref)
        assert iso._render_process is not None
        render_pid = iso._render_process.last_pid
        # 父进程、物理控制子进程与离屏渲染子进程三者 PID 互异。
        # Parent, physical control worker, and render worker must be three distinct OS PIDs.
        assert len({os.getpid(), iso.control_pid, render_pid}) == 3
    finally:
        iso.close()
        ref.close()
    assert iso._control_process.reaped and iso._control_process.last_exitcode == 0


async def test_isolated_sim_pick_place_contact_stop_and_perception(tmp_path):
    iso = IsolatedTabletopSim(seed=0)
    provider = build_perception_provider("color", isolated=True)
    perceived = PerceivedSim(iso, PerceptionPipeline(provider))
    registry = SkillRegistry(ledger=OperationLedger(tmp_path / "ops.sqlite3"), robot_id=iso.spec().embodiment_id)
    register_sim_skills(registry, perceived)
    try:
        pick_res = await registry.invoke("skill.manip.pick", {"object": "obj_cube"})
        assert pick_res.ok and pick_res.invocation_state == "RETURNED"
        assert held_object(WorldSnapshot.from_observation(iso.read())) == "obj_cube"

        # 持物中途触发受控取消，在隔离物理子进程内实测双侧接触力保持停止对账。
        # Interrupt while holding cube and verify bilateral contact-preserving stop reconciliation in the worker.
        entered, release = threading.Event(), threading.Event()

        def hold_and_wait():
            entered.set()
            release.wait(2.0)
            return SkillResult(ok=True)

        async def hold_handler():
            return await run_blocking(hold_and_wait)

        registry.register(
            SkillManifest(
                name="skill.test.hold",
                description="hold test",
                resources=["arm", "gripper", "workspace"],
                termination=TerminationSpec(timeout_s=0.1),
            ),
            hold_handler,
            tracked_workers=True,
        )
        op = OperationIdentity(task_id="t-stop", operation_id="op-hold-stop")
        task = asyncio.create_task(registry.invoke("skill.test.hold", operation=op))
        assert await asyncio.to_thread(entered.wait, 2.0)
        res = await task
        release.set()
        for _ in range(100):
            if registry._stop_contexts[op.operation_id].drained:
                break
            await asyncio.sleep(0.01)
        assert res.invocation_state == "OUTCOME_UNKNOWN"

        stopped = await registry.reconcile_stop(op.operation_id, res.attempt_id, confirmed=True)
        assert stopped.stop_state == "MOTION_STOPPED_OBSERVED"
        assert stopped.stop_evidence["gripper_strategy"] == "maintain_contact"
        assert stopped.stop_evidence["contact_objects"] == ["obj_cube"]
        assert held_object(WorldSnapshot.from_observation(iso.read())) == "obj_cube"

        place_res = await registry.invoke("skill.manip.place", {"region": "bin_region"})
        assert place_res.ok and place_res.invocation_state == "RETURNED"
    finally:
        await registry.drain_compute()
        registry.close()
        perceived.close()


async def test_mid_skill_native_gil_hang_contained_health_503_and_explicit_restart(tmp_path):
    fault_file = tmp_path / "fault.json"
    trace_file = tmp_path / "trace.txt"
    iso = IsolatedTabletopSim(
        seed=0,
        control_timeout_s=0.25,
        cancel_grace_s=0.12,
        _control_command=_real_fixture_command(fault_file, trace_file),
    )
    registry = SkillRegistry(ledger=OperationLedger(tmp_path / "ops.sqlite3"), robot_id=iso.spec().embodiment_id)
    register_sim_skills(registry, iso)
    ctx = AppContext(iso, registry, lambda _c: None, None, None)
    server = TestServer(build_app(ctx))
    client = TestClient(server)
    await client.start_server()
    try:
        # 健康时 /healthz 返回 200。 / Healthy worker reports 200 on /healthz.
        async with client.get("/healthz") as resp:
            assert resp.status == 200

        # 记录任务计划，再在第 2 次 step 注入原生持 GIL 挂起。
        # Record a task plan, then inject a native GIL hang on the 2nd step call.
        task_id = "task-hang-1"
        op_id = "op-hang-1"
        registry.record_task_plan({
            "task_id": task_id,
            "revision": 1,
            "goal": "pick cube",
            "steps": [{"id": "s1", "skill": "skill.manip.pick", "params": {"object": "obj_cube"}, "operation_id": op_id}],
        })
        registry.record_task_intent(task_id, 1, "s1", {"object": "obj_cube"})
        old_boot = iso.control_boot_id
        old_pid = iso.control_pid
        fault_file.write_text(json.dumps({"operation": "step", "after": 2, "fault": "gil_hang", "once": True}), encoding="utf-8")

        t0 = time.monotonic()
        res = await registry.invoke("skill.manip.pick", {"object": "obj_cube"}, operation=OperationIdentity(task_id=task_id, operation_id=op_id))
        elapsed = time.monotonic() - t0
        assert elapsed < 5.0
        assert not res.ok
        assert res.invocation_state == "OUTCOME_UNKNOWN"
        assert res.stop_state == "STOP_UNCONFIRMED"
        assert iso.control_process_state == "FAILED"
        assert iso._control_process.reaped
        assert iso.guard.halted
        assert registry._stop_contexts[op_id].drained

        # 父控制面保持响应：/healthz 报告 503 不可用，任务审查含 physical_control_unavailable 与 guard_unavailable。
        # Parent stays responsive: /healthz returns 503, and RecoveryReview reports physical_control_unavailable + guard_unavailable.
        async with client.get("/healthz") as resp:
            assert resp.status == 503
            body = await resp.json()
            assert body["reason"] == "physical_control_unavailable"
        async with client.get(f"/api/tasks/{task_id}?assess=1") as resp:
            assert resp.status == 200
            report = await resp.json()
            blockers = report["recovery_review"]["blockers"]
            assert "physical_control_unavailable" in blockers
            assert "guard_unavailable" in blockers
            assert report["recovery_review"]["execution_authorized"] is False

        # FAILED 期间拒绝 reconcile_stop、Guard.reset 与新调用。
        # While FAILED, reconcile_stop, guard.reset(), and new actions fail closed.
        with pytest.raises(StopRejected, match="physical_control_unavailable"):
            await registry.reconcile_stop(op_id, res.attempt_id, confirmed=True)
        with pytest.raises(PhysicalControlUnavailable):
            iso.guard.reset()

        # 显式重启物理控制子进程：轮换 boot_id 与 PID，同时严格保留 Guard 闩锁与资源隔离。
        # Explicit restart rotates boot_id and PID while preserving both Guard latch and resource quarantine.
        restarted = registry.restart_physical_control()
        assert restarted["boot_id"] != old_boot and restarted["pid"] != old_pid
        assert iso.control_process_state == "READY"
        assert iso.guard.halted and restarted["guard_halted"] is True
        assert iso.action_controller.boot_id == restarted["boot_id"]
        assert registry.arbiter.is_quarantined(op_id, ("arm", "gripper", "workspace"))

        # 旧代次效果上下文自动失效。 / Old generation effect context fails closed.
        with pytest.raises(EffectRejected, match="effect_stop_unconfirmed"):
            await registry.reconcile_effect_async(op_id, res.attempt_id)

        # 重启后可执行显式停止对账（不解除 Guard 闩锁、不翻转为 RETURNED）。
        # After restart, explicit stop reconciliation succeeds while keeping Guard latched and op OUTCOME_UNKNOWN.
        stop_rec = await registry.reconcile_stop(op_id, res.attempt_id, confirmed=True)
        assert stop_rec.state == "OUTCOME_UNKNOWN"
        assert stop_rec.stop_state == "MOTION_STOPPED_OBSERVED"
        assert iso.guard.halted
        # 停止已确认后，旧代次效果重查仍因 controller_boot_id 改变而被拒绝。
        # Even after stop confirmation, old effect reconciliation fails because controller_boot_id rotated.
        with pytest.raises(EffectRejected, match="effect_context_or_owner_changed"):
            await registry.reconcile_effect_async(op_id, res.attempt_id)

        # Guard 仍闩锁时新动作被拒绝并保守隔离；确认停止并显式 Guard.reset 后允许新任务执行。
        # While Guard stays latched, a new attempt fails and quarantines conservatively; after stop reconciliation and Guard.reset, a fresh op succeeds.
        halted_try = await registry.invoke("skill.arm.home")
        assert not halted_try.ok and halted_try.invocation_state == "OUTCOME_UNKNOWN"
        assert iso.guard.halted
        await registry.reconcile_stop(halted_try.operation_id, halted_try.attempt_id, confirmed=True)
        iso.guard.reset()
        assert not iso.guard.halted
        fresh = await registry.invoke("skill.arm.home")
        assert fresh.ok and fresh.invocation_state == "RETURNED"
        async with client.get("/healthz") as resp:
            assert resp.status == 200
    finally:
        await client.close()
        registry.close()
        iso.close()


@pytest.mark.parametrize("fault", ["crash", "abort"])
async def test_mid_skill_native_crash_or_abort_contains_failure(tmp_path, fault):
    fault_file = tmp_path / f"{fault}.json"
    trace_file = tmp_path / f"{fault}_trace.txt"
    iso = IsolatedTabletopSim(
        seed=0,
        control_timeout_s=2.0,
        _control_command=_real_fixture_command(fault_file, trace_file),
    )
    registry = SkillRegistry(ledger=OperationLedger(tmp_path / "ops.sqlite3"), robot_id=iso.spec().embodiment_id)
    register_sim_skills(registry, iso)
    try:
        fault_file.write_text(json.dumps({"operation": "write", "after": 1, "fault": fault, "once": True}), encoding="utf-8")
        res = await registry.invoke("skill.arm.home")
        assert not res.ok and res.invocation_state == "OUTCOME_UNKNOWN" and res.stop_state == "STOP_UNCONFIRMED"
        assert iso.control_process_state == "FAILED" and iso._control_process.reaped
        assert iso.guard.halted
        assert registry.arbiter.is_quarantined(res.operation_id, ("arm", "gripper", "workspace"))
    finally:
        await registry.drain_compute()
        registry.close()
        iso.close()


async def test_restart_rejects_asset_change_and_console_close_reaps_hung_physics(tmp_path):
    fault_file = tmp_path / "hang.json"
    trace_file = tmp_path / "hang_trace.txt"
    iso = IsolatedTabletopSim(
        seed=0,
        control_timeout_s=10.0,
        cancel_grace_s=0.1,
        _control_command=_real_fixture_command(fault_file, trace_file),
    )
    registry = SkillRegistry(ledger=OperationLedger(tmp_path / "ops.sqlite3"), robot_id=iso.spec().embodiment_id)
    register_sim_skills(registry, iso)
    ctx = AppContext(iso, registry, lambda _c: None, None, None)
    try:
        # 触发原生挂起并在技能运行中关闭控制台：drain_compute + cancel_control 须在有界时间内强杀回收。
        # Trigger native hang mid-skill and close console: drain_compute + cancel_control must reap in bounded time.
        fault_file.write_text(json.dumps({"operation": "step", "after": 1, "fault": "gil_hang", "once": True}), encoding="utf-8")
        op = OperationIdentity(task_id="t-close", operation_id="op-close-hang")
        started_skill = asyncio.create_task(registry.invoke("skill.arm.home", operation=op))
        deadline = time.monotonic() + 3.0
        while (not trace_file.exists() or "step:" not in trace_file.read_text(encoding="utf-8")) and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert iso._control_process.busy

        t0 = time.monotonic()
        await ctx.close()
        elapsed = time.monotonic() - t0
        assert elapsed < 3.0
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(started_skill, timeout=2.0)
        rec = registry.get_operation(op.operation_id)
        assert rec is not None and rec.state == "OUTCOME_UNKNOWN" and rec.stop_state == "STOP_UNCONFIRMED"
        assert iso.control_process_state == "FAILED" and iso._control_process.reaped

        # 篡改磁盘资产后，显式重启必须拒绝。 / Asset mutation on disk must block explicit restart.
        original_text = iso._scene_path.read_text(encoding="utf-8")
        try:
            iso._scene_path.write_text(original_text + "\n<!-- tampered -->\n", encoding="utf-8")
            with pytest.raises(PhysicalControlUnavailable, match="asset_changed"):
                registry.restart_physical_control()
        finally:
            iso._scene_path.write_text(original_text, encoding="utf-8")

        # 恢复原文件后，再确认不空闲时拒绝重启。 / Also verify ActionRejected when observation is busy.
        registry.add_read_barrier(lambda: True)
        with pytest.raises(ActionRejected, match="not_idle"):
            registry.restart_physical_control()
    finally:
        registry._read_barriers.clear()
        registry.close()
        iso.close()
