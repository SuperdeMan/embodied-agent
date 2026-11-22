"""Reproducible commit-bound P1 latency, stale-action, intervention, and episode integrity baseline (D035).

可重复且绑定提交的 P1 仿真停止延迟、过期动作拦截、人工介入与 Episode 硬杀完整性基线采集脚本（D035）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from embodied.contracts.metrics import (
    EpisodeIntegritySummary,
    P1MetricsReport,
    StaleActionInjectionSummary,
)
from embodied.contracts.operation import OperationIdentity
from embodied.control.actions import write_action
from embodied.control.hal import ActionCommand
from embodied.control.isolated_sim import IsolatedTabletopSim
from embodied.data_engine.recorder import EpisodeRecorder, audit_episode, recover_episode
from embodied.runtime.control_process import ControlProcess, PhysicalControlUnavailable
from embodied.runtime.invocation import run_blocking
from embodied.runtime.metrics import summarize_latencies_ms
from embodied.runtime.operations import OperationLedger
from embodied.skills.manifest import SkillManifest, TerminationSpec
from embodied.skills.registry import SkillRegistry, SkillResult
from embodied.skills.scripted.manip import register_sim_skills

ROOT = Path(__file__).resolve().parents[1]


def _git_info() -> tuple[str, bool]:
    commit = os.getenv("EMBODIED_RELEASE", "").strip()
    try:
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip()
        dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip())
        return sha, not dirty
    except Exception:
        # 容器内无 .git 时回退到镜像内嵌 RELEASE SHA。
        # Fall back to the embedded container RELEASE SHA when .git is absent.
        if len(commit) == 40:
            return commit, True
        return "0" * 40, False


def _stub_worker_cmd() -> list[str]:
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
        str(ROOT / "tests/runtime/control_worker_fixture.py"),
        "--mode",
        "stub",
    ]


async def _measure_stop_and_normal_runs(tmp: Path, *, stop_trials: int = 5, normal_episodes: int = 2):
    sim = IsolatedTabletopSim(seed=0)
    registry = SkillRegistry(ledger=OperationLedger(tmp / "ops.sqlite3"), robot_id=sim.spec().embodiment_id)
    register_sim_skills(registry, sim)
    rec = EpisodeRecorder(tmp / "episodes")
    normal_dirs: list[Path] = []
    try:
        # 1. 正常隔离物理控制抓放录制与动作/操作指标采集。
        # 1. Normal isolated-control pick+place with recording and action/operation metrics.
        for ep in range(normal_episodes):
            sim.reset(randomize=True)
            writer = rec.start("把红色方块放进盒子", sim.spec().embodiment_id, seed=ep, checkpoint_every=10)
            sim.set_hooks(
                on_step=writer.on_step,
                on_guard_event=lambda e, w=writer: w.on_event("safety", {"kind": e.kind, "reason": e.reason}),
            )
            try:
                r1 = await registry.invoke("skill.manip.pick", {"object": "obj_cube"})
                r2 = await registry.invoke("skill.manip.place", {"region": "bin_region"}) if r1.ok else SkillResult(ok=False)
                normal_dirs.append(writer.finish(bool(r1.ok and r2.ok), detail="metrics baseline"))
            finally:
                sim.set_hooks(None, None)

        normal_actions = sim.action_controller.metrics_snapshot()
        normal_ops = registry.metrics_snapshot()

        # 2. 真实隔离仿真中段打断 + 急停闩锁 + 真实停止对账耗时分布测量。
        # 2. Mid-motion interruption, Guard estop latch, and real isolated stop reconciliation latencies.
        for trial in range(stop_trials):
            sim.reset(randomize=False)
            entered, release = threading.Event(), threading.Event()

            def _move_worker():
                obs = sim.read_control_feedback()
                q = list(obs.qpos)
                q[0] += 0.08
                write_action(sim, ActionCommand(joint_targets=tuple(q)), observation=obs)
                sim.step(20)
                entered.set()
                release.wait(3.0)
                return SkillResult(ok=True)

            async def _handler():
                return await run_blocking(_move_worker)

            skill_name = f"skill.metrics.stop_{trial}"
            registry.register(
                SkillManifest(
                    name=skill_name,
                    description="stop latency probe",
                    resources=["arm", "gripper", "workspace"],
                    termination=TerminationSpec(timeout_s=0.12),
                ),
                _handler,
                tracked_workers=True,
            )
            op = OperationIdentity(task_id=f"metrics-stop-{trial}", operation_id=f"op-stop-{trial}")
            task = asyncio.create_task(registry.invoke(skill_name, operation=op))
            assert await asyncio.to_thread(entered.wait, 3.0)
            sim.guard.estop(f"metrics_trial_{trial}")
            res = await task
            release.set()
            for _ in range(200):
                if registry._stop_contexts[op.operation_id].drained:
                    break
                await asyncio.sleep(0.005)
            assert res.invocation_state == "OUTCOME_UNKNOWN"
            stop_rec = await registry.reconcile_stop(op.operation_id, res.attempt_id, confirmed=True)
            assert stop_rec.stop_state == "MOTION_STOPPED_OBSERVED"
            sim.guard.reset()

        guard_latch_summary = sim.latency_metrics.summary("guard_estop_latch_ms")
        stop_wall_summary = sim.latency_metrics.summary("stop_reconciliation_wall_ms")
        stop_sim_summary = sim.latency_metrics.summary("stop_reconciliation_sim_ms")
        return normal_dirs, normal_actions, normal_ops, guard_latch_summary, stop_wall_summary, stop_sim_summary
    finally:
        await registry.drain_compute()
        registry.close()
        sim.close()


def _measure_urgent_estop_kills(*, trials: int = 5):
    cmd = _stub_worker_cmd()
    kill_samples: list[float] = []
    for _ in range(trials):
        worker = ControlProcess({}, timeout_s=10.0, startup_timeout_s=10.0, cancel_grace_s=0.12, _command=cmd)
        try:
            worker.start()
            entered = threading.Event()
            err: list[BaseException] = []

            def _run():
                entered.set()
                try:
                    worker.call("step", {"fault": "gil_hang"})
                except BaseException as exc:
                    err.append(exc)

            th = threading.Thread(target=_run)
            th.start()
            assert entered.wait(2.0)
            deadline = time.monotonic() + 2.0
            while not worker.busy and time.monotonic() < deadline:
                time.sleep(0.005)
            t0 = time.monotonic()
            soft = worker.urgent_estop("metrics_hung_kill")
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            th.join(timeout=2.0)
            assert soft is False and not th.is_alive() and worker.state == "FAILED" and worker.reaped
            assert err and isinstance(err[0], PhysicalControlUnavailable)
            kill_samples.append(elapsed_ms)
        finally:
            worker.close()
    return summarize_latencies_ms(kill_samples)


async def _measure_stale_and_replay_interception(tmp: Path) -> StaleActionInjectionSummary:
    sim = IsolatedTabletopSim(seed=0)
    registry = SkillRegistry(ledger=OperationLedger(tmp / "stale.sqlite3"), robot_id=sim.spec().embodiment_id)
    register_sim_skills(registry, sim)
    stale_applied = 0
    stale_attempted = 0
    replay_attempted = 0
    replay_intercepted = 0
    try:
        def _stale_worker():
            nonlocal stale_applied, stale_attempted, replay_attempted, replay_intercepted
            obs = sim.read_control_feedback()
            q = tuple(obs.qpos)
            # 1. 正常基线动作与重放序号注入。 / 1. Valid baseline action followed by replayed sequence.
            env_ok = sim.prepare_action(ActionCommand(joint_targets=q))
            r_ok = sim.write_envelope(env_ok)
            assert r_ok.applied

            replay_attempted += 1
            r_rep = sim.write_envelope(env_ok)
            if not r_rep.applied and r_rep.reason == "action_sequence_replayed":
                replay_intercepted += 1
            else:
                stale_applied += 1
            return SkillResult(ok=True)

        async def _stale_suite_1():
            return await run_blocking(_stale_worker)

        registry.register(
            SkillManifest(name="skill.metrics.replay", description="replay probe", resources=["arm", "gripper", "workspace"]),
            _stale_suite_1,
            tracked_workers=True,
        )
        r = await registry.invoke("skill.metrics.replay")
        assert r.invocation_state == "OUTCOME_UNKNOWN"
        await registry.reconcile_stop(r.operation_id, r.attempt_id, confirmed=True)

        # 2. 分别注入：过期命令期限、陈旧控制反馈、陈旧语义目标。
        # 2. Inject expired command deadline, stale control observation, and stale semantic observation.
        for mode in ("expired_deadline", "stale_control_obs", "stale_semantic_obs"):
            def _inject(m=mode):
                nonlocal stale_applied, stale_attempted
                obs = sim.read_control_feedback()
                q = tuple(obs.qpos)
                stale_attempted += 1
                if m == "expired_deadline":
                    env = sim.prepare_action(ActionCommand(joint_targets=q))
                    expired = env.model_copy(
                        update={"produced_monotonic": env.produced_monotonic - 5.0, "valid_until_monotonic": env.produced_monotonic - 4.0}
                    )
                    res = sim.write_envelope(expired)
                elif m == "stale_control_obs":
                    stale_ref = obs.reference.model_copy(update={"captured_monotonic": obs.reference.captured_monotonic - 5.0})
                    sim.action_controller._references[stale_ref.reference_id] = stale_ref
                    env = sim.prepare_action(ActionCommand(joint_targets=q), reference=stale_ref)
                    res = sim.write_envelope(env)
                else:
                    obs.object_evidence = {}
                    res = write_action(sim, ActionCommand(joint_targets=q), observation=obs)
                if res.applied:
                    stale_applied += 1
                return SkillResult(ok=False, detail=res.reason)

            async def _h(fn=_inject):
                return await run_blocking(fn)

            name = f"skill.metrics.{mode}"
            registry.register(
                SkillManifest(name=name, description=mode, resources=["arm", "gripper", "workspace"]),
                _h,
                tracked_workers=True,
            )
            res = await registry.invoke(name)
            assert res.invocation_state == "OUTCOME_UNKNOWN"
            await registry.reconcile_stop(res.operation_id, res.attempt_id, confirmed=True)

        snap = sim.action_controller.metrics_snapshot()
        assert snap.stale_actions_rejected == stale_attempted and stale_applied == 0
        assert snap.replay_rejected == replay_attempted and replay_intercepted == replay_attempted
        return StaleActionInjectionSummary(
            injected_stale_attempts=stale_attempted,
            intercepted_stale_attempts=snap.stale_actions_rejected,
            stale_applied_count=stale_applied,
            stale_interception_rate=round(snap.stale_actions_rejected / stale_attempted, 6),
            injected_replay_attempts=replay_attempted,
            intercepted_replay_attempts=replay_intercepted,
        )
    finally:
        await registry.drain_compute()
        registry.close()
        sim.close()


def _measure_episode_integrity(tmp: Path, normal_dirs: list[Path], *, checkpoint_every: int = 10) -> EpisodeIntegritySummary:
    # 1. 审计正常录制目录 + 验证篡改 steps.npz 被检出。
    # 1. Audit normal recorded episodes and verify steps.npz tampering is detected.
    audits = [audit_episode(d) for d in normal_dirs]
    intact_count = sum(1 for a in audits if a.intact and a.steps_sha256_verified)
    normal_loss_rate = round(1.0 - (intact_count / len(audits)), 6) if audits else 0.0

    tamper_dir = normal_dirs[0]
    steps_file = tamper_dir / "steps.npz"
    original_bytes = steps_file.read_bytes()
    try:
        steps_file.write_bytes(original_bytes + b"\x00")
        tamper_audit = audit_episode(tamper_dir)
        tamper_detected = (not tamper_audit.intact) and ("steps_sha256_mismatch" in tamper_audit.issues)
    finally:
        steps_file.write_bytes(original_bytes)

    # 2. 在真实子进程中录制 57 步后直接 os._exit(9) 硬杀，验证增量日志恢复、截断尾行容忍与损失上界。
    # 2. Record 57 steps in a child process and hard-kill via os._exit(9); verify bounded recovery & torn-tail resilience.
    expected_steps = 57
    kill_root = tmp / "abrupt_kill"
    kill_root.mkdir(parents=True)
    child_code = "\n".join([
        "import os",
        "from pathlib import Path",
        "from embodied.control.hal import ActionCommand, Observation, Pose",
        "from embodied.data_engine.recorder import EpisodeRecorder",
        f"rec = EpisodeRecorder(Path({str(kill_root)!r}))",
        f"w = rec.start('crash probe', 'sim.mujoco.so_arm100', seed=0, checkpoint_every={checkpoint_every})",
        f"for i in range({expected_steps}):",
        "    obs = Observation(t=0.02*i, qpos=(0.01*i,0.0,0.0,0.0,0.0), qvel=(0.0,)*5, gripper_opening=0.5,",
        "                      ee_pose=Pose(pos=(0.1,0.0,0.2)), objects={'obj_cube': Pose(pos=(0.0,-0.2,0.015))})",
        "    w.on_step(obs, ActionCommand(joint_targets=(0.01*i,0.0,0.0,0.0,0.0), gripper=0.5))",
        "os._exit(9)",
    ])
    proc = subprocess.run([sys.executable, "-c", child_code], capture_output=True, text=True)
    assert proc.returncode == 9
    crashed_dir = next(kill_root.iterdir())
    pre_audit = audit_episode(crashed_dir)
    assert not pre_audit.intact and pre_audit.needs_recovery

    # 人为追加截断尾行模拟写盘中途掉电，确认恢复器丢弃残行并保留全部完整步骤。
    # Append a torn half-JSON line to simulate mid-flush power cut; recover must keep all intact rows.
    with open(crashed_dir / "steps.journal.jsonl", "ab") as fh:
        fh.write(b'{"t":9.99,"state":[1.0,2.0')

    recovered = recover_episode(crashed_dir)
    post_audit = audit_episode(crashed_dir)
    assert post_audit.intact and post_audit.recovered_from_crash and post_audit.steps_sha256_verified
    assert recovered.meta["dropped_corrupt_tail_lines"] == 1
    recovered_steps = len(recovered)
    lost_steps = expected_steps - recovered_steps
    bounded = 0 <= lost_steps < checkpoint_every
    return EpisodeIntegritySummary(
        checkpoint_every=checkpoint_every,
        normal_episodes_total=len(audits),
        normal_episodes_intact=intact_count,
        normal_data_loss_rate=normal_loss_rate,
        abrupt_kill_expected_steps=expected_steps,
        abrupt_kill_recovered_steps=recovered_steps,
        abrupt_kill_lost_steps=lost_steps,
        abrupt_kill_loss_rate=round(lost_steps / expected_steps, 6),
        abrupt_kill_bounded=bounded,
        torn_tail_tolerated=True,
        tamper_detected=tamper_detected,
    )


async def _run_all(args: argparse.Namespace) -> P1MetricsReport:
    commit, clean = _git_info()
    with tempfile.TemporaryDirectory(prefix="embodied_p1_metrics_") as raw_tmp:
        tmp = Path(raw_tmp)
        (
            normal_dirs,
            normal_actions,
            normal_ops,
            guard_latch,
            stop_wall,
            stop_sim,
        ) = await _measure_stop_and_normal_runs(tmp, stop_trials=args.stop_trials, normal_episodes=args.episodes)
        urgent_kill = _measure_urgent_estop_kills(trials=args.kill_trials)
        stale_summary = await _measure_stale_and_replay_interception(tmp)
        integrity = _measure_episode_integrity(tmp, normal_dirs)

    return P1MetricsReport(
        commit=commit,
        working_tree_clean=clean,
        platform=f"{platform.system()}-{platform.machine()}-py{platform.python_version()}",
        guard_estop_latch_ms=guard_latch,
        urgent_estop_kill_ms=urgent_kill,
        stop_reconciliation_wall_ms=stop_wall,
        stop_reconciliation_sim_ms=stop_sim,
        normal_run_actions=normal_actions,
        normal_run_operations=normal_ops,
        stale_action_injection=stale_summary,
        episode_integrity=integrity,
        boundaries=(
            "Measurements cover isolated MuJoCo simulation processes only; hardware_stop_measured is false",
            "urgent_estop_kill_ms includes cancel_grace_s (120 ms) before forcible process-tree SIGKILL/Job close",
            "Abrupt hard-kill recovery tests OS process exit and torn tail line, not physical storage-medium corruption",
            "p1_complete remains false until real-hardware SG-0 stop, cross-host vision clock, and v3 multi-attempt close",
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure commit-bound P1 latency, stale-action, and episode integrity baseline")
    parser.add_argument("--out", type=Path, default=ROOT / "outputs" / "p1-metrics")
    parser.add_argument("--report-file", type=Path, default=None, help="optional explicit JSON output path")
    parser.add_argument("--stop-trials", type=int, default=5)
    parser.add_argument("--kill-trials", type=int, default=5)
    parser.add_argument("--episodes", type=int, default=2)
    args = parser.parse_args()

    report = asyncio.run(_run_all(args))
    if args.report_file is not None:
        out_path = args.report_file
        out_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        run_dir = args.out / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + report.commit[:7])
        run_dir.mkdir(parents=True, exist_ok=False)
        out_path = run_dir / "report.json"

    out_path.write_text(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "report": str(out_path),
        "commit": report.commit,
        "guard_estop_latch_p95_ms": report.guard_estop_latch_ms.p95_ms,
        "urgent_estop_kill_p95_ms": report.urgent_estop_kill_ms.p95_ms,
        "stop_reconciliation_wall_p95_ms": report.stop_reconciliation_wall_ms.p95_ms,
        "stale_interception_rate": report.stale_action_injection.stale_interception_rate,
        "normal_data_loss_rate": report.episode_integrity.normal_data_loss_rate,
        "abrupt_kill_loss_rate": report.episode_integrity.abrupt_kill_loss_rate,
        "hardware_stop_measured": report.hardware_stop_measured,
        "p1_complete": report.p1_complete,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
