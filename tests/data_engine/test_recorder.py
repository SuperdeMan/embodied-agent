"""Recorder v0 contract: every run lands on disk complete, LeRobot-aligned, crash-safe."""

from __future__ import annotations

import json
import re

import numpy as np
import pytest

from embodied.control.hal import ActionCommand, Observation, Pose
from embodied.data_engine import EpisodeRecorder, load_episode, recorder, to_lerobot


def _obs(t=0.0, qpos=(0.1, 0.2, 0.3), grip=0.5, ee=(0.4, 0.0, 0.2), objects=None) -> Observation:
    return Observation(
        t=t,
        qpos=qpos,
        qvel=(0.0,) * len(qpos),
        gripper_opening=grip,
        ee_pose=Pose(pos=ee),
        objects=objects or {},
    )


def test_start_step_finish_roundtrip(tmp_path):
    rec = EpisodeRecorder(tmp_path)
    w = rec.start("pick the cube", "sim.mujoco.so_arm100", seed=7, extra_meta={"scene": "table"})
    objects = {"cube": Pose(pos=(0.3, 0.0, 0.05))}
    for i in range(4):
        w.on_step(
            _obs(t=0.1 * i, qpos=(0.1 + i, 0.2, 0.3), objects=objects),
            ActionCommand(joint_targets=(0.1 * i, 0.0, 0.0), gripper=1.0),
        )
    path = w.finish(True, "done")

    assert path.parent == tmp_path
    assert re.fullmatch(r"\d{8}-\d{6}-\d{3}", path.name)

    with np.load(path / "steps.npz") as z:
        assert set(z.files) == {"timestamp", "observation.state", "action", "ee_pos", "object/cube"}
        assert z["timestamp"].shape == (4,) and z["timestamp"].dtype == np.float32
        assert z["observation.state"].shape == (4, 4) and z["observation.state"].dtype == np.float32
        assert z["action"].shape == (4, 4) and z["action"].dtype == np.float32
        assert z["ee_pos"].shape == (4, 3) and z["ee_pos"].dtype == np.float32
        assert z["object/cube"].shape == (4, 7) and z["object/cube"].dtype == np.float32

    meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
    assert meta["schema_version"] == "v0"
    assert meta["task"] == "pick the cube"
    assert meta["embodiment_id"] == "sim.mujoco.so_arm100"
    assert meta["seed"] == 7
    assert meta["success"] is True
    assert meta["detail"] == "done"
    assert meta["length"] == 4
    assert meta["aborted"] is False
    assert meta["started_at"] and meta["finished_at"]
    assert meta["extra_meta"] == {"scene": "table"}
    assert meta["lerobot_field_map"] == {
        "observation.state": "observation.state",
        "action": "action",
        "timestamp": "timestamp",
        "meta.task": "tasks",
    }

    ep = load_episode(path)
    assert len(ep) == 4
    assert ep.meta == meta
    assert ep.events == []
    np.testing.assert_array_equal(ep.timestamp, np.asarray([0.1 * i for i in range(4)], dtype=np.float32))
    np.testing.assert_array_equal(
        ep.state, np.asarray([[0.1 + i, 0.2, 0.3, 0.5] for i in range(4)], dtype=np.float32)
    )
    np.testing.assert_array_equal(
        ep.action, np.asarray([[0.1 * i, 0.0, 0.0, 1.0] for i in range(4)], dtype=np.float32)
    )
    np.testing.assert_array_equal(ep.ee_pos, np.asarray([[0.4, 0.0, 0.2]] * 4, dtype=np.float32))
    np.testing.assert_array_equal(
        ep.objects["cube"], np.asarray([[0.3, 0.0, 0.05, 1.0, 0.0, 0.0, 0.0]] * 4, dtype=np.float32)
    )


def test_action_carry_forward(tmp_path):
    w = EpisodeRecorder(tmp_path).start("t", "sim.test")
    w.on_step(_obs(t=0.0), None)  # before any action -> zeros
    w.on_step(_obs(t=0.1), ActionCommand(joint_targets=(1.0, 2.0, 3.0), gripper=0.7))
    w.on_step(_obs(t=0.2), None)  # None action -> carry full previous row
    w.on_step(_obs(t=0.3), ActionCommand(joint_targets=(4.0, 5.0, 6.0)))  # gripper None -> carry gripper
    path = w.finish(False)

    np.testing.assert_array_equal(
        load_episode(path).action,
        np.asarray(
            [
                [0.0, 0.0, 0.0, 0.0],
                [1.0, 2.0, 3.0, 0.7],
                [1.0, 2.0, 3.0, 0.7],
                [4.0, 5.0, 6.0, 0.7],
            ],
            dtype=np.float32,
        ),
    )


def test_events_stream_as_json_lines(tmp_path):
    w = EpisodeRecorder(tmp_path).start("t", "sim.test")
    w.on_event("skill_start", {"skill": "skill.arm.reach", "sim_t": 1.25})
    w.on_event("safety", {"reason": "workspace_limit"})
    w.on_event("note", {"text": "操作员备注"})  # non-ASCII payload must survive utf-8

    lines = (w.path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    records = [json.loads(line) for line in lines]
    assert [r["kind"] for r in records] == ["skill_start", "safety", "note"]
    assert all(isinstance(r["t"], float) for r in records)
    assert records[0]["sim_t"] == 1.25
    assert records[0]["payload"] == {"skill": "skill.arm.reach"}
    assert "sim_t" not in records[1]
    assert records[2]["payload"] == {"text": "操作员备注"}

    path = w.finish(True)
    assert load_episode(path).events == records


def test_invalid_event_kind_raises(tmp_path):
    w = EpisodeRecorder(tmp_path).start("t", "sim.test")
    with pytest.raises(ValueError, match="skill_oops"):
        w.on_event("skill_oops", {})
    assert not (w.path / "events.jsonl").exists()
    w.abort("cleanup")


def test_abort_preserves_episode(tmp_path):
    w = EpisodeRecorder(tmp_path).start("t", "sim.test")
    w.on_step(_obs(t=0.0), None)
    path = w.abort("sim exploded")

    meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
    assert meta["success"] is False
    assert meta["aborted"] is True
    assert "sim exploded" in meta["detail"]
    assert load_episode(path).state.shape == (1, 4)  # buffered steps not lost

    assert w.abort("again") == path  # crash path stays callable after close
    with pytest.raises(RuntimeError):
        w.finish(True)


def test_finish_twice_raises_and_writer_locks(tmp_path):
    w = EpisodeRecorder(tmp_path).start("t", "sim.test")
    w.on_step(_obs(), None)
    w.finish(True)
    with pytest.raises(RuntimeError):
        w.finish(True)
    with pytest.raises(RuntimeError):
        w.on_step(_obs(), None)
    with pytest.raises(RuntimeError):
        w.on_event("note", {})


def test_same_second_collision_increments_seq(tmp_path, monkeypatch):
    monkeypatch.setattr(recorder, "_stamp", lambda: "20260805-120000")
    rec = EpisodeRecorder(tmp_path)
    a = rec.start("t", "sim.test")
    b = rec.start("t", "sim.test")
    assert a.path.name == "20260805-120000-001"
    assert b.path.name == "20260805-120000-002"
    a.abort("cleanup")
    b.abort("cleanup")


def test_episodes_dir_env_honored(tmp_path, monkeypatch):
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path / "custom"))
    w = EpisodeRecorder().start("t", "sim.test")
    assert w.path.parent == tmp_path / "custom"
    w.finish(True)


def test_explicit_root_beats_env(tmp_path, monkeypatch):
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path / "env"))
    assert EpisodeRecorder(tmp_path / "explicit").root == tmp_path / "explicit"


def test_default_root_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv("EPISODES_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    w = EpisodeRecorder().start("t", "sim.test")
    assert w.path.parent.resolve() == (tmp_path / "outputs" / "episodes").resolve()
    w.finish(True)


def test_object_appearing_late_is_nan_padded(tmp_path):
    w = EpisodeRecorder(tmp_path).start("t", "sim.test")
    w.on_step(_obs(t=0.0), None)
    w.on_step(_obs(t=0.1, objects={"cube": Pose(pos=(1.0, 2.0, 3.0))}), None)
    w.on_step(_obs(t=0.2), None)  # cube gone again

    cube = load_episode(w.finish(True)).objects["cube"]
    assert cube.shape == (3, 7)
    assert np.isnan(cube[0]).all() and np.isnan(cube[2]).all()
    np.testing.assert_array_equal(cube[1], np.asarray([1, 2, 3, 1, 0, 0, 0], dtype=np.float32))


def test_empty_episode_finishes(tmp_path):
    ep = load_episode(EpisodeRecorder(tmp_path).start("t", "sim.test").finish(False, "nothing happened"))
    assert len(ep) == 0
    assert ep.meta["length"] == 0
    assert ep.meta["success"] is False


def test_start_writes_meta_stub_immediately(tmp_path):
    """Hard-kill durability: task identity is on disk before the first step."""
    w = EpisodeRecorder(tmp_path).start("t", "sim.test", seed=1)
    meta = json.loads((w.path / "meta.json").read_text(encoding="utf-8"))
    assert meta["success"] is None and meta["finished_at"] is None
    assert meta["task"] == "t" and meta["seed"] == 1
    w.abort("cleanup")


def test_to_lerobot_stub_raises(tmp_path):
    with pytest.raises(NotImplementedError, match="D012"):
        to_lerobot(tmp_path, tmp_path / "out")
