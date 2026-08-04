"""WorldState v0: typed world snapshot + spatial predicates.

Ground truth flows from the sim driver for now; perception fills the same shapes
from M2 on. Skills verify their own effects against snapshots ("did the world
actually change") — the planner-level verify contract arrives with the engine port.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from embodied.control.hal import Observation, Pose

Vec3 = tuple[float, float, float]


@dataclass(frozen=True)
class Region:
    center: Vec3
    half: Vec3

    def contains(self, pos: Vec3, margin: float = 0.0) -> bool:
        return all(abs(pos[i] - self.center[i]) <= self.half[i] + margin for i in range(3))


@dataclass
class WorldSnapshot:
    t: float
    ee_pos: Vec3
    gripper_opening: float
    objects: dict[str, Pose] = field(default_factory=dict)
    regions: dict[str, Region] = field(default_factory=dict)

    @classmethod
    def from_observation(cls, obs: Observation) -> "WorldSnapshot":
        regions = {
            name: Region(center=tuple(r["center"]), half=tuple(r["half"]))
            for name, r in obs.extras.get("regions", {}).items()
        }
        return cls(
            t=obs.t,
            ee_pos=obs.ee_pose.pos,
            gripper_opening=obs.gripper_opening,
            objects=dict(obs.objects),
            regions=regions,
        )


def object_in_region(snap: WorldSnapshot, obj: str, region: str, margin: float = 0.0) -> bool:
    if obj not in snap.objects or region not in snap.regions:
        return False
    return snap.regions[region].contains(snap.objects[obj].pos, margin=margin)


def object_near(snap: WorldSnapshot, obj: str, pos: Vec3, radius: float) -> bool:
    if obj not in snap.objects:
        return False
    p = snap.objects[obj].pos
    return math.dist(p, pos) <= radius
