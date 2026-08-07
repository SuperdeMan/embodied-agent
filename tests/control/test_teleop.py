"""Teleop session over the real sim driver. Auto-skips like the other sim tests
(CI has no mujoco/assets; run locally via `uv run --group sim pytest tests/control`)."""

from __future__ import annotations

import json
import math

import pytest

mujoco = pytest.importorskip("mujoco")

from embodied.control.simassets import find_menagerie_model  # noqa: E402

try:
    find_menagerie_model()
    _HAS_ASSETS = True
except FileNotFoundError:
    _HAS_ASSETS = False

pytestmark = pytest.mark.skipif(not _HAS_ASSETS, reason="menagerie assets not fetched")

from embodied.control.teleop import TELEOP_COLLECTOR_ID, TeleopSession  # noqa: E402


@pytest.fixture(scope="module")
def sim():
    from embodied.control.drivers.mujoco_sim import TabletopSim

    return TabletopSim(seed=42)


def test_jog_moves_ee_and_records(sim, tmp_path):
    session = TeleopSession(sim, task="teleop smoke", root=tmp_path)
    session.start_episode(randomize=False)
    before = sim.read().ee_pose.pos
    result = session.jog(0.0, 0.0, 0.02)  # 2 cm up: safely inside fence and reach
    after = sim.read().ee_pose.pos
    assert result.ok, result.reason
    moved = math.dist(before, after)
    assert 0.005 < moved < 0.05, f"expected ~2cm motion, got {moved * 1000:.1f}mm"

    path = session.finish_episode(True)
    meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
    assert meta["success"] is True and meta["aborted"] is False
    assert meta["extra_meta"]["collector"] == TELEOP_COLLECTOR_ID
    assert meta["extra_meta"]["state_names"][-1] == "Jaw"
    assert meta["length"] > 0  # hooks streamed steps into the episode


def test_fence_jog_refused_without_motion(sim, tmp_path):
    session = TeleopSession(sim, task="t", root=tmp_path, record=False)
    session.start_episode(randomize=False)
    before = sim.read().ee_pose.pos
    result = session.jog(0.0, 0.0, 5.0)  # far above the workspace fence
    assert not result.ok and result.reason in ("fence", "unreachable")
    assert math.dist(before, sim.read().ee_pose.pos) < 1e-6  # fail closed: no motion at all


def test_gripper_toggle_roundtrip(sim, tmp_path):
    session = TeleopSession(sim, task="t", root=tmp_path, record=False)
    session.start_episode(randomize=False)
    start = sim.read().gripper_opening
    assert session.toggle_gripper()
    mid = sim.read().gripper_opening
    assert session.toggle_gripper()
    end = sim.read().gripper_opening
    assert abs(mid - start) > 0.15  # visibly moved
    assert abs(end - start) < abs(mid - start)  # and came back toward the start side


def test_dangling_episode_kept_as_failed(sim, tmp_path):
    session = TeleopSession(sim, task="t", root=tmp_path)
    session.start_episode(randomize=False)
    first = session.recorder.root
    session.close()
    dirs = sorted(p for p in first.iterdir() if p.is_dir())
    assert dirs, "aborted episode directory must survive"
    meta = json.loads((dirs[-1] / "meta.json").read_text(encoding="utf-8"))
    assert meta["success"] is False and meta["aborted"] is True
