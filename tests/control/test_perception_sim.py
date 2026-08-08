"""Perception v1 over the real sim driver: depth render, truth-vs-perceived accuracy,
and the perception-driven closed loop (D015). Auto-skips without mujoco/assets."""

from __future__ import annotations

import math

import numpy as np
import pytest

mujoco = pytest.importorskip("mujoco")

from embodied.control.simassets import find_menagerie_model  # noqa: E402

try:
    find_menagerie_model()
    _HAS_ASSETS = True
except FileNotFoundError:
    _HAS_ASSETS = False

pytestmark = pytest.mark.skipif(not _HAS_ASSETS, reason="menagerie assets not fetched")

from embodied.cognition.perception import (  # noqa: E402
    TABLETOP_OBJECTS,
    PerceivedSim,
    PerceptionPipeline,
)
from embodied.cognition.world_state import WorldSnapshot, object_in_region  # noqa: E402
from embodied.providers.perception import ColorBlobProvider  # noqa: E402


@pytest.fixture(scope="module")
def sim():
    from embodied.control.drivers.mujoco_sim import TabletopSim

    return TabletopSim(seed=7)


def test_render_rgbd_shapes_and_plausible_depth(sim):
    sim.reset()
    rgb, depth = sim.render_rgbd("top", width=320, height=240)
    assert rgb.shape == (240, 320, 3) and rgb.dtype == np.uint8
    assert depth.shape == (240, 320) and depth.dtype == np.float32
    center = depth[100:140, 140:180]
    assert 0.3 < float(np.median(center)) < 1.2  # camera sits 0.75 m above the table


def test_perceived_position_close_to_truth(sim):
    """Multi-camera fallback (the wrapper's strategy): the arm occludes parts of the
    sector from any single view, so detection may need the second camera."""
    pipeline = PerceptionPipeline(ColorBlobProvider(), dict(TABLETOP_OBJECTS))
    worst = 0.0
    for _ in range(5):
        sim.reset(randomize=True)
        truth = np.asarray(sim.read().objects["obj_cube"].pos)
        poses: dict = {}
        for camera in ("top", "side"):
            rgb, depth = sim.render_rgbd(camera, width=640, height=480)
            cam = sim.camera_model(camera, width=640, height=480)
            poses = pipeline.locate(rgb, depth, cam)
            if "obj_cube" in poses:
                break
        assert "obj_cube" in poses, "red cube must be visible from at least one camera"
        err = float(np.linalg.norm(np.asarray(poses["obj_cube"].pos) - truth))
        worst = max(worst, err)
    assert worst <= 0.02, f"perception error {worst * 1000:.1f}mm exceeds 20mm budget"


def test_perceived_sim_agent_vs_judge_views(sim):
    wrapped = PerceivedSim(sim, PerceptionPipeline(ColorBlobProvider(), dict(TABLETOP_OBJECTS)))
    wrapped.reset(randomize=True)
    agent = wrapped.read()
    judge = wrapped.read_truth()
    assert agent.qpos == judge.qpos  # proprioception is shared truth
    a, j = np.asarray(agent.objects["obj_cube"].pos), np.asarray(judge.objects["obj_cube"].pos)
    assert 0.0 < float(np.linalg.norm(a - j)) <= 0.02  # close, but genuinely perceived


def test_perception_driven_pick_place_closed_loop(sim):
    """The M2 DoD line: scripted skills acting ONLY on perceived objects, success
    judged from ground truth."""
    from embodied.skills.registry import SkillRegistry
    from embodied.skills.scripted.manip import register_sim_skills

    wrapped = PerceivedSim(sim, PerceptionPipeline(ColorBlobProvider(), dict(TABLETOP_OBJECTS)))
    registry = SkillRegistry()
    register_sim_skills(registry, wrapped)

    import asyncio

    wins = 0
    for _ in range(3):
        wrapped.reset(randomize=True)
        pick = asyncio.run(registry.invoke("skill.manip.pick", {"object": "obj_cube"}))
        place = asyncio.run(registry.invoke("skill.manip.place", {"region": "bin_region"}))
        snap = WorldSnapshot.from_observation(wrapped.read_truth())
        ok = object_in_region(snap, "obj_cube", "bin_region", margin=0.005)
        wins += int(ok and pick.ok and place.ok)
    assert wins >= 2, f"perception-driven closed loop won {wins}/3"


def test_spawn_override_confines_radius(sim):
    from embodied.control.drivers.mujoco_sim import TabletopSim

    deep = TabletopSim(seed=3, spawn_radial=(0.24, 0.27))
    for _ in range(5):
        obs = deep.reset(randomize=True)
        x, y, _ = obs.objects["obj_cube"].pos
        assert 0.235 <= math.hypot(x, y) <= 0.275
