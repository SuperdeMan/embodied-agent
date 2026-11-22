"""Episode incremental journal, SHA-256 audit, tamper detection, and abrupt-kill recovery tests (D035).

Episode 增量日志、SHA-256 完整性审计、篡改检测与真实子进程硬杀恢复测试（D035）。
"""

from __future__ import annotations

import json
import subprocess
import sys

import numpy as np
import pytest

from embodied.control.hal import ActionCommand, Observation, Pose
from embodied.data_engine.recorder import (
    STEP_JOURNAL_FILENAME,
    EpisodeRecorder,
    audit_episode,
    load_episode,
    recover_episode,
)


def _obs(t: float, q0: float, *, with_cube: bool = True) -> Observation:
    objs = {"obj_cube": Pose(pos=(0.0, -0.2, 0.015))} if with_cube else {}
    return Observation(
        t=t,
        qpos=(q0, 0.1, 0.2),
        qvel=(0.0, 0.0, 0.0),
        gripper_opening=0.5,
        ee_pose=Pose(pos=(0.1, 0.0, 0.2)),
        objects=objs,
    )


def test_normal_episode_audit_sha256_and_tamper_detection(tmp_path):
    rec = EpisodeRecorder(tmp_path)
    w = rec.start("pick cube", "sim.mujoco.so_arm100", seed=1, checkpoint_every=5)
    for i in range(12):
        w.on_step(_obs(0.02 * i, 0.01 * i), ActionCommand(joint_targets=(0.01 * i, 0.1, 0.2), gripper=0.5))
    # 超过 checkpoint_every 后增量日志已落盘。 / Incremental journal lands on disk once checkpoint_every is reached.
    assert (w.path / STEP_JOURNAL_FILENAME).is_file()
    w.on_event("note", {"kind": "ok"})
    ep_dir = w.finish(True, "completed")

    # 正常收尾后临时日志清理，meta 含 steps_sha256，审计通过。
    # Normal finalization removes transient journal, records steps_sha256, and passes audit.
    assert not (ep_dir / STEP_JOURNAL_FILENAME).exists()
    verdict = audit_episode(ep_dir)
    assert verdict.intact and not verdict.needs_recovery and verdict.steps_sha256_verified
    assert verdict.recorded_steps == 12 and verdict.events_count == 1 and verdict.issues == ()

    # 篡改 steps.npz 必须被 SHA-256 检出。 / Tampering steps.npz must fail SHA-256 audit.
    steps_path = ep_dir / "steps.npz"
    steps_path.write_bytes(steps_path.read_bytes() + b"corrupt")
    tampered = audit_episode(ep_dir)
    assert not tampered.intact and not tampered.steps_sha256_verified
    assert "steps_sha256_mismatch" in tampered.issues


def test_real_subprocess_abrupt_kill_recovers_bounded_steps_and_tolerates_torn_tail(tmp_path):
    checkpoint_every = 10
    expected_steps = 47
    child_code = "\n".join([
        "import os",
        "from pathlib import Path",
        "from embodied.control.hal import ActionCommand, Observation, Pose",
        "from embodied.data_engine.recorder import EpisodeRecorder",
        f"rec = EpisodeRecorder(Path({str(tmp_path)!r}))",
        f"w = rec.start('hard kill test', 'sim.mujoco.so_arm100', seed=3, checkpoint_every={checkpoint_every})",
        f"for i in range({expected_steps}):",
        "    objs = {'obj_cube': Pose(pos=(0.0, -0.2, 0.015))} if i >= 5 else {}",
        "    obs = Observation(t=0.02*i, qpos=(0.01*i, 0.1, 0.2), qvel=(0.0, 0.0, 0.0), gripper_opening=0.5,",
        "                      ee_pose=Pose(pos=(0.1, 0.0, 0.2)), objects=objs)",
        "    w.on_step(obs, ActionCommand(joint_targets=(0.01*i, 0.1, 0.2), gripper=0.5))",
        "os._exit(9)",
    ])
    proc = subprocess.run([sys.executable, "-c", child_code], capture_output=True, text=True)
    assert proc.returncode == 9

    crashed_dir = next(tmp_path.iterdir())
    pre = audit_episode(crashed_dir)
    assert not pre.intact and pre.needs_recovery
    assert "missing_steps_npz" in pre.issues and "unfinalized_meta" in pre.issues

    # 模拟刷盘中途掉电留下的截断尾行。 / Simulate a torn JSONL tail left by power loss mid-write.
    with open(crashed_dir / STEP_JOURNAL_FILENAME, "ab") as fh:
        fh.write(b'{"t":9.99,"state":[0.1,0.2')

    recovered = recover_episode(crashed_dir)
    assert len(recovered) == 40
    lost = expected_steps - len(recovered)
    assert 0 <= lost < checkpoint_every
    assert recovered.meta["success"] is False
    assert recovered.meta["aborted"] is True
    assert recovered.meta["recovered_from_crash"] is True
    assert recovered.meta["dropped_corrupt_tail_lines"] == 1
    assert not (crashed_dir / STEP_JOURNAL_FILENAME).exists()

    # 中途第 5 步才出现的物体前 5 行须正确 NaN 对齐。 / Object appearing at step 5 must keep NaN-padded prefix.
    assert np.isnan(recovered.objects["obj_cube"][:5]).all()
    assert np.isfinite(recovered.objects["obj_cube"][5:]).all()

    post = audit_episode(crashed_dir)
    assert post.intact and not post.needs_recovery and post.recovered_from_crash and post.steps_sha256_verified
    # 重复恢复保持幂等。 / Repeated recovery is idempotent.
    again = recover_episode(crashed_dir)
    assert len(again) == len(recovered)
    np.testing.assert_array_equal(again.state, recovered.state)


def test_invalid_checkpoint_every_and_corrupt_events_audit(tmp_path):
    rec = EpisodeRecorder(tmp_path)
    with pytest.raises(ValueError, match="positive integer"):
        rec.start("bad", "sim.test", checkpoint_every=0)

    w = rec.start("events", "sim.test")
    w.on_step(_obs(0.0, 0.0), ActionCommand(joint_targets=(0.0, 0.1, 0.2), gripper=0.5))
    ep_dir = w.finish(True)
    meta = json.loads((ep_dir / "meta.json").read_text(encoding="utf-8"))
    assert len(load_episode(ep_dir)) == meta["length"]
    (ep_dir / "events.jsonl").write_text("{not-json\n", encoding="utf-8")
    verdict = audit_episode(ep_dir)
    assert not verdict.intact and "corrupt_events_jsonl" in verdict.issues
