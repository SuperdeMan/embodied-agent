"""Mock skills: prove the see-think-act loop end to end before any embodiment exists (M0 DoD).

Replaced by sim-backed scripted skills in M1; the manifests' shape is the part that lasts.
"""

from __future__ import annotations

import asyncio

from embodied.skills.manifest import ParamSpec, SkillManifest
from embodied.skills.registry import SkillRegistry, SkillResult

HOME = SkillManifest(
    name="skill.arm.home",
    description="Move the arm to its home (rest) pose.",
    effects=["arm at home pose"],
    scopes=["arm.home"],
)


async def _home() -> SkillResult:
    await asyncio.sleep(0)  # placeholder for real motion
    return SkillResult(ok=True, detail="arm homed", data={"pose": "home"})


WAVE = SkillManifest(
    name="skill.arm.wave",
    description="Wave the gripper as a greeting gesture.",
    params={"times": ParamSpec(type="integer", description="how many waves", required=False, default=2)},
    scopes=["arm.move"],
)


async def _wave(times: int = 2) -> SkillResult:
    return SkillResult(ok=True, detail=f"waved {times} times", data={"times": times})


POWER_OFF = SkillManifest(
    name="skill.system.power_off",
    description="Power down the robot controller.",
    require_confirm=True,
)


async def _power_off() -> SkillResult:
    return SkillResult(ok=True, detail="controller powered off (mock)")


def register_builtin(registry: SkillRegistry) -> None:
    registry.register(HOME, _home)
    registry.register(WAVE, _wave)
    registry.register(POWER_OFF, _power_off)
