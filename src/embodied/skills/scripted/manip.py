"""Scripted manipulation skills for the tabletop sim: home / pick / place.

Every skill verifies its declared effect against a fresh WorldSnapshot before
claiming success (car-agent's outcome-verification discipline: motion finishing
≠ task done — a slipped grasp must report ok=False so the planner can react).
Handlers wrap blocking sim motion in asyncio.to_thread to keep the agent loop live.


桌面脚本技能回零、抓取和放置在报告成功前核对新世界快照；阻塞运动用线程执行，保持 Agent 循环响应。
"""

from __future__ import annotations

import asyncio
from typing import Any

import numpy as np

from embodied.cognition.world_state import WorldSnapshot, held_object, object_in_region
from embodied.skills.manifest import ParamSpec, SkillManifest, TerminationSpec
from embodied.skills.registry import SkillRegistry, SkillResult
from embodied.skills.scripted import motions

PREGRASP_DZ = 0.08
# 中途重新瞄准高度位于接触之上且低于易推移区域。
MID_DZ = 0.035  # re-aim height: below nudge-risk, above contact
# 抓取点略高于中心，使指垫夹住下半部且指尖不触地。
GRASP_DZ = 0.010  # site above object center: pads grip the lower half without fingertips striking the floor
# 固定指位于抓取点正 X 侧，向该侧偏移使其掠过物体外侧，由活动指完成闭合。
# The gripper has a FIXED finger on the site's +x side: aim the site slightly to that
# side of the object so the fixed finger slides past the object face instead of landing
# on top of it; the moving finger then does all the closing travel.
# 仿真网格扫描发现偏移 10 毫米仍碰撞，14 毫米抓取可靠。
GRASP_LATERAL_OFF = 0.014  # grid-swept in sim: +10mm still strikes the cube, +14mm grasps reliably
# 完全张开会使活动指下降时触地。
OPEN_FOR_GRASP = 0.6  # full open swings the moving jaw low enough to strike the floor during descent
OPEN_FOR_RELEASE = 0.8
HOVER_Z = 0.13
DROP_Z = 0.09

HOME = SkillManifest(
    name="skill.arm.home",
    resources=["arm", "gripper", "workspace"],
    description="Move the arm to its home rest pose.",
    effects=["arm at home pose"],
    scopes=["arm.home"],
    termination=TerminationSpec(timeout_s=30.0),
)

PICK = SkillManifest(
    name="skill.manip.pick",
    resources=["arm", "gripper", "workspace"],
    description="Pick up a named object from the table with a top grasp.",
    params={"object": ParamSpec(type="string", description="object id, e.g. obj_cube", required=False, default="obj_cube")},
    preconditions=["object visible on table", "gripper empty"],
    effects=["object grasped and lifted"],
    scopes=["arm.move", "gripper.close"],
    termination=TerminationSpec(timeout_s=60.0, success="object held above table"),
    verification={
        "mode": "state_predicate",
        "expect": {"predicate": "gripper_holding", "args": {"object": "$param:object"}},
        "timeout_ms": 1500,
        "on_fail": "report",
    },
)

PLACE = SkillManifest(
    name="skill.manip.place",
    resources=["arm", "gripper", "workspace"],
    description="Place the currently held object into a named region (e.g. the bin).",
    params={"region": ParamSpec(type="string", description="target region id", required=False, default="bin_region")},
    preconditions=["an object is grasped"],
    effects=["object inside target region"],
    scopes=["arm.move", "gripper.open"],
    termination=TerminationSpec(timeout_s=60.0, success="object center inside region box"),
    # 放置内部空间检查通过才返回物体与区域；外层结构验证确认真实结果存在，避免重复同一空间谓词。
    # Schema mode: the skill returns {object, region} only after its own in-skill region
    # check passed — asserting real data present exercises the second evaluator without
    # duplicating the spatial predicate (which pick already demonstrates).
    verification={"mode": "schema", "expect": {"data_keys": ["object", "region"]}, "on_fail": "report"},
)


def _snap(emb: Any) -> WorldSnapshot:
    return WorldSnapshot.from_observation(emb.read())


def _jaw_x_world(emb: Any) -> np.ndarray:
    """World direction of the grasp frame's +x (fixed-finger side), horizontalized.

    计算抓取框架固定指一侧正 X 的水平世界方向。
    """
    w, x, y, z = emb.read().ee_pose.quat
    xa = np.array([1 - 2 * (y * y + z * z), 2 * (x * y + w * z), 2 * (x * z - w * y)])
    xa[2] = 0.0
    n = np.linalg.norm(xa)
    return xa / n if n > 1e-6 else np.array([1.0, 0.0, 0.0])


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
    # 两段下降在中间高度重新观测并瞄准，修正扇区边缘斜向接近时固定指推动物体的偏差。
    # Two-stage descent with mid-height re-aim: at oblique approach angles (sector edges)
    # the fixed finger can clip and shove the object during descent; whatever the first
    # stage did to it, the final stage re-reads the LIVE pose and aims there.
    fresh = np.asarray(_snap(emb).objects[obj].pos)
    mid_target = fresh + GRASP_LATERAL_OFF * _jaw_x_world(emb) + [0, 0, MID_DZ]
    if not motions.reach(emb, mid_target, max_pos_err=0.008, seconds=0.8, gripper=OPEN_FOR_GRASP):
        return SkillResult(ok=False, detail="mid descent unreachable or denied")
    fresh2 = np.asarray(_snap(emb).objects[obj].pos)
    grasp_target = fresh2 + GRASP_LATERAL_OFF * _jaw_x_world(emb) + [0, 0, GRASP_DZ]
    if not motions.reach(emb, grasp_target, max_pos_err=0.006, seconds=0.7, gripper=OPEN_FOR_GRASP):
        return SkillResult(ok=False, detail="grasp descent unreachable or denied")
    if not motions.set_gripper(emb, 0.0, seconds=0.6):
        return SkillResult(ok=False, detail="guard denied gripper close")
    # 抬升返回已知预抓姿态，避免深抓配置下朝下轴逆解病态。
    # Lift by returning to the known pregrasp posture: no IK from the deep grasp
    # configuration, where the axis-down solve is poorly conditioned.
    if not motions.move_to_q(emb, pregrasp.qpos, seconds=0.9):
        return SkillResult(ok=False, detail="lift denied")
    after = _snap(emb)
    held = held_object(after)
    if held != obj:
        return SkillResult(ok=False, detail=f"grasp verification failed (held={held})", data={"object": obj})
    return SkillResult(ok=True, detail=f"{obj} grasped and lifted", data={"object": obj})


def _place_sync(emb: Any, region: str) -> SkillResult:
    snap = _snap(emb)
    if region not in snap.regions:
        return SkillResult(ok=False, detail=f"unknown region {region!r}")
    held = held_object(snap)
    if held is None:
        return SkillResult(ok=False, detail="nothing grasped")
    cx, cy, _ = snap.regions[region].center
    if not motions.reach(emb, (cx, cy, HOVER_Z), max_pos_err=0.012):
        return SkillResult(ok=False, detail="hover unreachable or denied")
    if not motions.reach(emb, (cx, cy, DROP_Z), max_pos_err=0.012, seconds=0.8):
        return SkillResult(ok=False, detail="lower unreachable or denied")
    if not motions.set_gripper(emb, OPEN_FOR_RELEASE, seconds=0.5, settle_steps=200):
        return SkillResult(ok=False, detail="guard denied gripper open")
    # 尽力撤离。
    motions.reach(emb, (cx, cy, HOVER_Z + 0.03), max_pos_err=0.015, seconds=0.6)  # retreat, best-effort
    # 等待物体稳定。
    emb.step(150)  # let the object come to rest
    after = _snap(emb)
    if not object_in_region(after, held, region, margin=0.005):
        return SkillResult(ok=False, detail=f"{held} not in {region} after release", data={"object": held})
    return SkillResult(ok=True, detail=f"{held} placed in {region}", data={"object": held, "region": region})


def register_sim_skills(registry: SkillRegistry, emb: Any, *, skip: tuple[str, ...] = ()) -> None:
    """Register the scripted skills. ``skip`` names manifests to leave unregistered so a
    learned implementation can claim them instead (skills.policies, same manifest —
    architecture §4.3 impl swap; the registry rejects duplicates by design).

    按 skip 排除将由学习实现占用的 manifest，保留名称与契约不变。
    """

    async def home() -> SkillResult:
        ok = await asyncio.to_thread(motions.move_to_q, emb, emb.home_qpos(), seconds=1.5)
        return SkillResult(ok=ok, detail="arm homed" if ok else "guard denied motion")

    async def pick(object: str = "obj_cube") -> SkillResult:
        return await asyncio.to_thread(_pick_sync, emb, object)

    async def place(region: str = "bin_region") -> SkillResult:
        return await asyncio.to_thread(_place_sync, emb, region)

    for manifest, handler in ((HOME, home), (PICK, pick), (PLACE, place)):
        if manifest.name not in skip:
            registry.register(manifest, handler)
