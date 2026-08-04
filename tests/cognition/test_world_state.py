from __future__ import annotations

from embodied.cognition.world_state import Region, WorldSnapshot, object_in_region, object_near
from embodied.control.hal import Observation, Pose


def make_snap() -> WorldSnapshot:
    obs = Observation(
        t=1.0,
        qpos=(0.0,),
        qvel=(0.0,),
        gripper_opening=0.5,
        ee_pose=Pose(pos=(0.0, 0.0, 0.1)),
        objects={"obj_cube": Pose(pos=(0.16, -0.18, 0.02))},
        extras={"regions": {"bin_region": {"center": (0.16, -0.18, 0.045), "half": (0.045, 0.045, 0.04)}}},
    )
    return WorldSnapshot.from_observation(obs)


def test_from_observation_builds_regions():
    snap = make_snap()
    assert snap.regions["bin_region"] == Region(center=(0.16, -0.18, 0.045), half=(0.045, 0.045, 0.04))


def test_object_in_region():
    snap = make_snap()
    assert object_in_region(snap, "obj_cube", "bin_region")
    assert not object_in_region(snap, "obj_cube", "missing")
    assert not object_in_region(snap, "missing", "bin_region")


def test_margin_semantics():
    snap = make_snap()
    snap.objects["obj_cube"] = Pose(pos=(0.16 + 0.046, -0.18, 0.045))  # 1mm outside
    assert not object_in_region(snap, "obj_cube", "bin_region")
    assert object_in_region(snap, "obj_cube", "bin_region", margin=0.002)


def test_object_near():
    snap = make_snap()
    assert object_near(snap, "obj_cube", (0.16, -0.18, 0.02), radius=0.01)
    assert not object_near(snap, "obj_cube", (0.0, 0.0, 0.0), radius=0.05)
