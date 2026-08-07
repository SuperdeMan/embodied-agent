"""v0 -> LeRobotDataset conversion. Auto-skips without the learn group (like tests/rpc);
CI never installs lerobot, locally these MUST run (uv sync --group learn)."""

from __future__ import annotations

import numpy as np
import pytest

lerobot = pytest.importorskip("lerobot", reason="learn group not installed (uv sync --group learn)")

from lerobot.datasets.lerobot_dataset import LeRobotDataset  # noqa: E402

from embodied.control.hal import ActionCommand, Observation, Pose  # noqa: E402
from embodied.data_engine.lerobot_convert import convert_episodes  # noqa: E402
from embodied.data_engine.recorder import EpisodeRecorder  # noqa: E402

DT = 0.02  # 50 Hz capture cadence in the fixture episodes


def obs(t: float, j0: float, grip: float) -> Observation:
    return Observation(
        t=t, qpos=(j0, 0.5), qvel=(0.0, 0.0), gripper_opening=grip,
        ee_pose=Pose(pos=(0.0, 0.0, 0.2)),
        objects={"obj_b": Pose(pos=(t, 0.0, 0.0)), "obj_a": Pose(pos=(0.0, t, 0.0))},
    )


def write_episode(root, *, success: bool = True, boundaries: bool = True, task: str = "do the thing"):
    """One synthetic pick(0.02-0.54s) + settle gap + place(1.0-1.5s) episode.

    joint0 ramps linearly with sim time (lerp-checkable); actions are the staircase
    of written targets (zoh-checkable). The settle gap (0.54 -> 1.0) mimics a single
    step(n) call producing one late sample.
    """
    w = EpisodeRecorder(root).start(
        task=task, embodiment_id="sim.fake", seed=0,
        extra_meta={"state_names": ["j0", "j1", "gripper"]},
    )
    times = [k * DT for k in range(1, 28)] + [1.0] + [1.0 + k * DT for k in range(1, 26)]
    if boundaries:
        w.on_event("skill_start", {"skill": "skill.fake.pick", "sim_t": times[0]})
    for t in times:
        if boundaries and abs(t - 1.0) < 1e-9:
            w.on_event("skill_end", {"skill": "skill.fake.pick", "sim_t": 0.54})
            w.on_event("skill_start", {"skill": "skill.fake.place", "sim_t": 1.0})
        w.on_step(obs(t, j0=t, grip=0.5), ActionCommand(joint_targets=(t, 0.5), gripper=0.5))
    if boundaries:
        w.on_event("skill_end", {"skill": "skill.fake.place", "sim_t": times[-1]})
    return w.finish(success, detail="fixture")


def test_episode_mode_round_trip(tmp_path):
    root = tmp_path / "episodes"
    write_episode(root, success=True)
    write_episode(root, success=True)
    write_episode(root, success=False)  # excluded by default
    out = tmp_path / "ds"

    report = convert_episodes(root, out, fps=50, progress=lambda s: None)
    assert report.episodes_converted == 2 and report.segments_written == 2
    assert any("success=False" in r for _, r in report.skipped)

    ds = LeRobotDataset(repo_id="local/ds", root=out)
    assert ds.meta.total_episodes == 2
    assert ds.meta.fps == 50
    assert set(ds.meta.tasks.index) == {"do the thing"}
    feats = ds.meta.features
    assert tuple(feats["observation.state"]["shape"]) == (3,) and tuple(feats["action"]["shape"]) == (3,)
    assert feats["observation.state"]["names"] == ["j0", "j1", "gripper"]
    # environment_state: sorted object order (obj_a before obj_b), 7 dims each
    assert tuple(feats["observation.environment_state"]["shape"]) == (14,)
    assert feats["observation.environment_state"]["names"][0] == "obj_a.x"
    assert feats["observation.environment_state"]["names"][7] == "obj_b.x"

    item = ds[0]
    assert item["observation.state"].shape == (3,)
    assert item["observation.environment_state"].shape == (14,)
    # first grid point sits at the first sample: j0 == t == DT
    assert abs(float(item["observation.state"][0]) - DT) < 1e-5
    # obj_a moves along y with t; obj_b along x
    assert abs(float(item["observation.environment_state"][1]) - DT) < 1e-5
    assert abs(float(item["observation.environment_state"][7]) - DT) < 1e-5


def test_resampling_lerp_and_zoh_across_gap(tmp_path):
    root = tmp_path / "episodes"
    write_episode(root, success=True)
    out = tmp_path / "ds"
    convert_episodes(root, out, fps=50, progress=lambda s: None)
    ds = LeRobotDataset(repo_id="local/ds", root=out)

    # Uniform grid from t0=0.02: index k -> sim time 0.02 + k/50. Inside the settle
    # gap (0.54 -> 1.0): state lerps between the flanking samples, action holds 0.54.
    k = 30  # sim time 0.62, inside the gap
    item = ds[k]
    sim_t = DT + k / 50
    assert abs(float(item["observation.state"][0]) - sim_t) < 1e-4  # lerp of a linear ramp
    assert abs(float(item["action"][0]) - 0.54) < 1e-5  # zero-order hold at gap entry
    # timestamps restart at 0 per dataset episode and step exactly 1/fps
    assert abs(float(ds[0]["timestamp"])) < 1e-6
    assert abs(float(ds[1]["timestamp"]) - 0.02) < 1e-6


def test_skill_segmentation(tmp_path):
    root = tmp_path / "episodes"
    write_episode(root, success=True)
    out = tmp_path / "ds"
    report = convert_episodes(root, out, fps=50, segment="skill", progress=lambda s: None)
    assert report.episodes_converted == 1 and report.segments_written == 2

    ds = LeRobotDataset(repo_id="local/ds", root=out)
    assert ds.meta.total_episodes == 2
    assert set(ds.meta.tasks.index) == {"skill.fake.pick", "skill.fake.place"}
    # pick spans 0.02..0.54 -> 27 frames; place spans 1.0..1.5 -> 26 frames
    lengths = sorted(int(ds.meta.episodes[i]["length"]) for i in range(ds.meta.total_episodes))
    assert lengths == [26, 27]


def test_skill_mode_skips_pre_m2_recordings(tmp_path):
    root = tmp_path / "episodes"
    write_episode(root, success=True, boundaries=False)
    with pytest.raises(ValueError, match="no convertible episodes"):
        convert_episodes(root, tmp_path / "ds", segment="skill", progress=lambda s: None)
    assert not (tmp_path / "ds").exists()  # transactional: nothing half-written


def test_include_failures_and_existing_out_dir(tmp_path):
    root = tmp_path / "episodes"
    write_episode(root, success=False)
    out = tmp_path / "ds"
    report = convert_episodes(root, out, include_failures=True, progress=lambda s: None)
    assert report.episodes_converted == 1
    with pytest.raises(FileExistsError, match="immutable"):
        convert_episodes(root, out, include_failures=True, progress=lambda s: None)


def test_layout_mismatch_fails_loudly(tmp_path):
    root = tmp_path / "episodes"
    write_episode(root, success=True)
    w = EpisodeRecorder(root).start(task="odd", embodiment_id="sim.fake")
    for k in range(1, 8):
        t = k * DT
        w.on_step(  # 1 arm joint instead of 2 -> state dim 2, and different objects
            Observation(t=t, qpos=(t,), qvel=(0.0,), gripper_opening=0.5,
                        ee_pose=Pose(pos=(0, 0, 0.2)), objects={"obj_x": Pose(pos=(t, 0, 0))}),
            ActionCommand(joint_targets=(t,), gripper=0.5),
        )
    w.finish(True)
    with pytest.raises(ValueError, match="layout mismatch"):
        convert_episodes(root, tmp_path / "ds", progress=lambda s: None)
    assert not (tmp_path / "ds").exists()


def test_nan_padded_objects_get_filled(tmp_path):
    root = tmp_path / "episodes"
    w = EpisodeRecorder(root).start(task="t", embodiment_id="sim.fake")
    for k in range(1, 30):
        t = k * DT
        objects = {"obj_late": Pose(pos=(t, 0, 0))} if k >= 10 else {}
        w.on_step(
            Observation(t=t, qpos=(t, 0.0), qvel=(0.0, 0.0), gripper_opening=0.5,
                        ee_pose=Pose(pos=(0, 0, 0.2)), objects=objects),
            ActionCommand(joint_targets=(t, 0.0), gripper=0.5),
        )
    w.finish(True)
    out = tmp_path / "ds"
    convert_episodes(root, out, progress=lambda s: None)
    ds = LeRobotDataset(repo_id="local/ds", root=out)
    env = np.stack([ds[i]["observation.environment_state"].numpy() for i in range(len(ds))])
    assert not np.isnan(env).any()  # NaN prefix back-filled from first sighting
