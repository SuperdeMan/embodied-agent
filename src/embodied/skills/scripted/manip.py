"""Scripted manipulation skills for the tabletop sim: home / pick / place.

Every skill verifies its declared effect against a fresh WorldSnapshot before
claiming success (car-agent's outcome-verification discipline: motion finishing
≠ task done — a slipped grasp must report ok=False so the planner can react).
Handlers wrap blocking sim motion in asyncio.to_thread to keep the agent loop live.
"""

from __future__ import annotations

import asyncio
import math
from typing import Any

import numpy as np

from embodied.cognition.world_state import WorldSnapshot, object_in_region
from embodied.skills.manifest import ParamSpec, SkillManifest, TerminationSpec
from embodied.skills.registry import SkillRegistry, SkillResult
from embodied.skills.scripted import motions

PREGRASP_DZ = 0.08
GRASP_DZ = 0.010  # site above object center: pads grip the lower half without fingertips striking the floor
# The gripper has a FIXED finger on the site's +x side: aim the site slightly to that
# side of the object so the fixed finger slides past the object face instead of landing
# on top of it; the moving finger then does all the closing travel.
GRASP_LATERAL_OFF = 0.014  # grid-swept in sim: +10mm still strikes the cube, +14mm grasps reliably
OPEN_FOR_GRASP = 0.6  # full open swings the moving jaw low enough to strike the floor during descent
OPEN_FOR_RELEASE = 0.8
HOVER_Z = 0.13
DROP_Z = 0.09
GRASP_HOLD_RADIUS = 0.06
GRASP_MIN_Z = 0.055

HOME = SkillManifest(
    name="skill.arm.home",
    description="Move the arm to its home rest pose.",
    effects=["arm at home pose"],
    scopes=["arm.home"],
    termination=TerminationSpec(timeout_s=30.0),
)

PICK = SkillManifest(
    name="skill.manip.pick",
    description="Pick up a named object from the table with a top grasp.",
    params={"object": ParamSpec(type="string", description="object id, e.g. obj_cube", required=False, default="obj_cube")},
    preconditions=["object visible on table", "gripper empty"],
    effects=["object grasped and lifted"],
    scopes=["arm.move", "gripper.close"],
    termination=TerminationSpec(timeout_s=60.0, success="object held above table"),
)

PLACE = SkillManifest(
    name="skill.manip.place",
    description="Place the currently held object into a named region (e.g. the bin).",
    params={"region": ParamSpec(type="string", description="target region id", required=False, default="bin_region")},
    preconditions=["an object is grasped"],
    effects=["object inside target region"],
    scopes=["arm.move", "gripper.open"],
    termination=TerminationSpec(timeout_s=60.0, success="object center inside region box"),
)


def _snap(emb: Any) -> WorldSnapshot:
    return WorldSnapshot.from_observation(emb.read())


def _jaw_x_world(emb: Any) -> np.ndarray:
    """World direction of the grasp frame's +x (fixed-finger side), horizontalized."""
    w, x, y, z = emb.read().ee_pose.quat
    xa = np.array([1 - 2 * (y * y + z * z), 2 * (x * y + w * z), 2 * (x * z - w * y)])
    xa[2] = 0.0
    n = np.linalg.norm(xa)
    return xa / n if n > 1e-6 else np.array([1.0, 0.0, 0.0])


def _held_object(snap: WorldSnapshot) -> str | None:
    for name, pose in snap.objects.items():
        if pose.pos[2] > GRASP_MIN_Z and math.dist(pose.pos, snap.ee_pos) < GRASP_HOLD_RADIUS:
            return name
    return None


def _pick_sync(emb: Any, obj: str) -> SkillResult:
    snap = _snap(emb)
    if obj not in snap.objects:
        return SkillResult(ok=False, detail=f"unknown object {obj!r}")
    target = np.asarray(snap.objects[obj].pos)

    pregrasp = emb.solve_reach(target + [0, 0, PREGRASP_DZ])
    if pregrasp.pos_err > 0.008:
        return SkillResult(ok=False, detail=f"pregrasp unreachable ({pregrasp.pos_err * 1000:.0f}mm)")
    if not emb.guard.check_target(tuple(target + [0, 0, GRASP_DZ])):
        return SkillResult(ok=False, detail="grasp target outside safety fence")
    if not motions.set_gripper(emb, OPEN_FOR_GRASP):
        return SkillResult(ok=False, detail="guard denied gripper open")
    if not motions.move_to_q(emb, pregrasp.qpos, gripper=OPEN_FOR_GRASP):
        return SkillResult(ok=False, detail="pregrasp motion denied")
    fresh = np.asarray(_snap(emb).objects[obj].pos)  # descent aims at the (possibly nudged) live pose
    grasp_target = fresh + GRASP_LATERAL_OFF * _jaw_x_world(emb) + [0, 0, GRASP_DZ]
    if not motions.reach(emb, grasp_target, max_pos_err=0.006, seconds=1.2, gripper=OPEN_FOR_GRASP):
        return SkillResult(ok=False, detail="grasp descent unreachable or denied")
    if not motions.set_gripper(emb, 0.0, seconds=0.6):
        return SkillResult(ok=False, detail="guard denied gripper close")
    # Lift by returning to the known pregrasp posture: no IK from the deep grasp
    # configuration, where the axis-down solve is poorly conditioned.
    if not motions.move_to_q(emb, pregrasp.qpos, seconds=0.9):
        return SkillResult(ok=False, detail="lift denied")
    after = _snap(emb)
    held = _held_object(after)
    if held != obj:
        return SkillResult(ok=False, detail=f"grasp verification failed (held={held})", data={"object": obj})
    return SkillResult(ok=True, detail=f"{obj} grasped and lifted", data={"object": obj})


def _place_sync(emb: Any, region: str) -> SkillResult:
    snap = _snap(emb)
    if region not in snap.regions:
        return SkillResult(ok=False, detail=f"unknown region {region!r}")
    held = _held_object(snap)
    if held is None:
        return SkillResult(ok=False, detail="nothing grasped")
    cx, cy, _ = snap.regions[region].center
    if not motions.reach(emb, (cx, cy, HOVER_Z), max_pos_err=0.012):
        return SkillResult(ok=False, detail="hover unreachable or denied")
    if not motions.reach(emb, (cx, cy, DROP_Z), max_pos_err=0.012, seconds=0.8):
        return SkillResult(ok=False, detail="lower unreachable or denied")
    if not motions.set_gripper(emb, OPEN_FOR_RELEASE, seconds=0.5, settle_steps=200):
        return SkillResult(ok=False, detail="guard denied gripper open")
    motions.reach(emb, (cx, cy, HOVER_Z + 0.03), max_pos_err=0.015, seconds=0.6)  # retreat, best-effort
    emb.step(150)  # let the object come to rest
    after = _snap(emb)
    if not object_in_region(after, held, region, margin=0.005):
        return SkillResult(ok=False, detail=f"{held} not in {region} after release", data={"object": held})
    return SkillResult(ok=True, detail=f"{held} placed in {region}", data={"object": held, "region": region})


def register_sim_skills(registry: SkillRegistry, emb: Any) -> None:
    async def home() -> SkillResult:
        ok = await asyncio.to_thread(motions.move_to_q, emb, emb.home_qpos(), seconds=1.5)
        return SkillResult(ok=ok, detail="arm homed" if ok else "guard denied motion")

    async def pick(object: str = "obj_cube") -> SkillResult:
        return await asyncio.to_thread(_pick_sync, emb, object)

    async def place(region: str = "bin_region") -> SkillResult:
        return await asyncio.to_thread(_place_sync, emb, region)

    registry.register(HOME, home)
    registry.register(PICK, pick)
    registry.register(PLACE, place)
