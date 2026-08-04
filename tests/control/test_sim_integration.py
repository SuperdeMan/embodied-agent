"""Sim driver + skills integration tests. Auto-skip when mujoco or the fetched
assets are absent (CI runs without the sim group; run locally via
`uv run --group sim pytest tests/control`)."""

from __future__ import annotations

import pytest

mujoco = pytest.importorskip("mujoco")

from embodied.control.simassets import find_menagerie_model  # noqa: E402

try:
    find_menagerie_model()
    _HAS_ASSETS = True
except FileNotFoundError:
    _HAS_ASSETS = False

pytestmark = pytest.mark.skipif(not _HAS_ASSETS, reason="menagerie assets not fetched")


@pytest.fixture(scope="module")
def sim():
    from embodied.control.drivers.mujoco_sim import TabletopSim

    return TabletopSim(seed=123)


def test_spec_shape(sim):
    spec = sim.spec()
    assert spec.embodiment_id == "sim.mujoco.so_arm100"
    assert [j.name for j in spec.joints] == ["Rotation", "Pitch", "Elbow", "Wrist_Pitch", "Wrist_Roll"]
    assert spec.gripper_joint is not None


def test_reset_places_objects_not_at_origin(sim):
    obs = sim.reset()
    cube = obs.objects["obj_cube"].pos
    assert abs(cube[0]) + abs(cube[1]) > 0.1, "keyframe zero-padding regression: cube teleported to base"
    assert obs.extras["regions"]["bin_region"]["half"] == (0.045, 0.045, 0.04)


def test_randomize_within_sector(sim):
    for i in range(5):
        obs = sim.reset(randomize=True)
        x, y, _ = obs.objects["obj_cube"].pos
        r = (x * x + y * y) ** 0.5
        assert 0.15 <= r <= 0.30
        assert y < 0  # forward sector


def test_guard_gates_writes(sim):
    from embodied.control.hal import ActionCommand

    sim.reset()
    res = sim.write(ActionCommand(joint_targets=tuple(sim.arm_qpos()), source="not_whitelisted"))
    assert not res.applied and "source_not_whitelisted" in res.reason


def test_ik_reaches_object(sim):
    sim.reset()
    cube = sim.object_pos("obj_cube")
    res = sim.solve_reach(cube + [0, 0, 0.08])
    assert res.converged and res.pos_err < 0.005


async def test_pick_place_end_to_end(sim):
    """The M1 vertical slice in one test: fixed layout pick&place through the
    skill registry, with effect verification against world state."""
    from embodied.skills.registry import SkillRegistry
    from embodied.skills.scripted.manip import register_sim_skills

    sim.reset()
    registry = SkillRegistry()
    register_sim_skills(registry, sim)
    r1 = await registry.invoke("skill.manip.pick", {})
    assert r1.ok, r1.detail
    r2 = await registry.invoke("skill.manip.place", {})
    assert r2.ok, r2.detail
