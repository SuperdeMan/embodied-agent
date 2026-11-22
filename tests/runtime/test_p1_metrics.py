"""Contract and instrumentation tests for P1 latency, stale-action, and intervention metrics (D035).

P1 延迟分布、过期动作拦截计数与人工介入率契约与运行时采样测试（D035）。
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from embodied.contracts.metrics import (
    ActionMetricsSnapshot,
    LatencySummary,
    OperationMetricsSnapshot,
)
from embodied.contracts.operation import OperationIdentity
from embodied.control.actions import write_action
from embodied.control.hal import ActionCommand
from embodied.runtime.invocation import run_blocking
from embodied.runtime.metrics import LatencyRecorder, summarize_latencies_ms
from embodied.skills.manifest import SkillManifest, TerminationSpec
from embodied.skills.registry import ConfirmationRequired, SkillRegistry, SkillResult


def test_latency_summary_quantiles_and_validation():
    empty = summarize_latencies_ms([])
    assert empty.count == 0 and empty.p50_ms is None and empty.p95_ms is None

    summary = summarize_latencies_ms([10.0, 2.0, 8.0, 4.0, 6.0, 1.0, 3.0, 5.0, 7.0, 9.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0, 18.0, 19.0, 20.0])
    assert summary.count == 20
    assert summary.min_ms == 1.0
    assert summary.p50_ms == 10.0
    assert summary.p95_ms == 19.0
    assert summary.max_ms == 20.0

    with pytest.raises(ValueError, match="finite and non-negative"):
        summarize_latencies_ms([-1.0])
    with pytest.raises(ValueError, match="finite and non-negative"):
        summarize_latencies_ms([float("nan")])
    with pytest.raises(ValueError, match="non-decreasing"):
        LatencySummary(count=2, min_ms=5.0, p50_ms=3.0, p95_ms=5.0, max_ms=6.0)


def test_latency_recorder_ring_buffer_and_ignores_invalid():
    rec = LatencyRecorder(max_samples=3)
    rec.record_ms("stop", 10.0)
    rec.record_ms("stop", 20.0)
    rec.record_ms("stop", 30.0)
    rec.record_ms("stop", 40.0)
    rec.record_ms("stop", float("inf"))
    rec.record_ms("stop", -5.0)
    assert rec.samples("stop") == (20.0, 30.0, 40.0)
    snap = rec.snapshot()
    assert snap["stop"]["count"] == 3 and snap["stop"]["max_ms"] == 40.0


async def test_registry_metrics_track_outcomes_and_interventions():
    registry = SkillRegistry()

    async def ok_skill():
        return SkillResult(ok=True)

    async def fail_skill():
        return SkillResult(ok=False, detail="failed")

    registry.register(SkillManifest(name="skill.test.ok", description="ok"), ok_skill)
    registry.register(SkillManifest(name="skill.test.fail", description="fail"), fail_skill)
    registry.register(SkillManifest(name="skill.test.danger", description="danger", require_confirm=True), ok_skill)

    try:
        assert (await registry.invoke("skill.test.ok")).ok
        assert not (await registry.invoke("skill.test.fail")).ok
        with pytest.raises(ConfirmationRequired):
            await registry.invoke("skill.test.danger")
        assert (await registry.invoke("skill.test.danger", confirmed=True)).ok

        snap = registry.metrics_snapshot()
        assert isinstance(snap, OperationMetricsSnapshot)
        assert snap.operations_started == 3
        assert snap.operations_returned == 2
        assert snap.operations_failed == 1
        assert snap.operations_unknown == 0
        assert snap.confirmations_denied == 1
        assert snap.manual_intervention_rate == pytest.approx(1 / 3, abs=1e-5)
    finally:
        registry.close()


async def test_isolated_sim_records_action_stale_and_stop_latency_metrics():
    pytest.importorskip("mujoco")
    from embodied.control.isolated_sim import IsolatedTabletopSim
    from embodied.control.simassets import find_menagerie_model
    from embodied.skills.scripted.manip import register_sim_skills

    try:
        find_menagerie_model()
    except FileNotFoundError:
        pytest.skip("menagerie assets not fetched")

    sim = IsolatedTabletopSim(seed=0)
    registry = SkillRegistry(robot_id=sim.spec().embodiment_id)
    register_sim_skills(registry, sim)
    try:
        entered, release = threading.Event(), threading.Event()

        def worker():
            obs = sim.read_control_feedback()
            q = tuple(obs.qpos)
            # 1. 合法写入计数。 / 1. Valid write counter.
            env = sim.prepare_action(ActionCommand(joint_targets=q))
            assert sim.write_envelope(env).applied
            sim.step(10)
            # 2. 过期命令拒绝计数。 / 2. Expired command rejection counter.
            expired = env.model_copy(
                update={"sequence": env.sequence + 1, "produced_monotonic": env.produced_monotonic - 5.0,
                        "valid_until_monotonic": env.produced_monotonic - 4.0}
            )
            res = sim.write_envelope(expired)
            assert not res.applied and res.reason == "action_expired_or_invalid_deadline"
            entered.set()
            release.wait(2.0)
            return SkillResult(ok=False)

        async def handler():
            return await run_blocking(worker)

        registry.register(
            SkillManifest(name="skill.test.stale", description="stale", resources=["arm", "gripper", "workspace"],
                          termination=TerminationSpec(timeout_s=0.1)),
            handler,
            tracked_workers=True,
        )
        op = OperationIdentity(task_id="t-m", operation_id="op-m")
        task = asyncio.create_task(registry.invoke("skill.test.stale", operation=op))
        assert await asyncio.to_thread(entered.wait, 2.0)
        sim.guard.estop("metrics_test")
        res = await task
        release.set()
        for _ in range(200):
            if registry._stop_contexts[op.operation_id].drained:
                break
            await asyncio.sleep(0.005)
        await registry.reconcile_stop(op.operation_id, res.attempt_id, confirmed=True)

        action_snap = sim.action_controller.metrics_snapshot()
        assert isinstance(action_snap, ActionMetricsSnapshot)
        assert action_snap.actions_accepted == 1
        assert action_snap.stale_actions_rejected == 1
        assert action_snap.actions_rejected >= 1
        assert action_snap.stale_action_rate > 0.0

        assert sim.latency_metrics.summary("guard_estop_latch_ms").count >= 1
        assert sim.latency_metrics.summary("stop_reconciliation_wall_ms").count == 1
        assert sim.latency_metrics.summary("stop_reconciliation_sim_ms").min_ms >= 99.0

        op_snap = registry.metrics_snapshot()
        assert op_snap.operations_unknown == 1
        assert op_snap.stop_reconciliations_succeeded == 1
        # 测试语义过期计数路径。 / Also exercise semantic_observation_stale rejection accounting.
        sim.guard.reset()
        obs = sim.read_control_feedback()
        obs.object_evidence = {}
        sem_res = write_action(sim, ActionCommand(joint_targets=tuple(obs.qpos)), observation=obs)
        assert not sem_res.applied and sem_res.reason == "semantic_observation_stale"
        assert sim.action_controller.metrics_snapshot().stale_actions_rejected == 2
    finally:
        await registry.drain_compute()
        registry.close()
        sim.close()
