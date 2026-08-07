"""Learned-skill runtime: policies behind the SAME manifests as scripted skills.

Architecture §4.3 — one contract, two implementations: replacing a scripted skill
with a learned policy swaps the registered handler, never the manifest, so the
planner cannot tell the difference (roadmap M2 "manifest unchanged").

The runner is deliberately dumb: read obs -> build the policy inputs (layout is
BY CONSTRUCTION the converter's: state = qpos + gripper_opening, environment_state
= sorted-name object poses, docs/decisions.md D014) -> stream the action chunk
through the guarded write path at the policy's control rate. Success is judged by
the same ground-truth predicates the scripted skills use — a policy never
self-reports success (D009). Guard vetoes fail the rollout closed.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

import numpy as np

from embodied.cognition.world_state import (
    WorldSnapshot,
    gripper_holding,
    held_object,
    object_in_region,
)
from embodied.control.hal import ActionCommand
from embodied.providers.policy import BasePolicyProvider
from embodied.skills.registry import SkillRegistry, SkillResult
from embodied.skills.scripted.manip import PICK, PLACE

MAX_PICK_SECONDS = 12.0  # dataset mean pick segment ~5.6 s; 2x headroom
MAX_PLACE_SECONDS = 10.0  # dataset mean place segment ~3.5 s
PLACE_MARGIN_M = 0.005  # same judge margin as the scripted skill and the eval task
SETTLE_STEPS = 150  # matches scripted place: let the object come to rest before judging


def _snap(emb: Any) -> WorldSnapshot:
    return WorldSnapshot.from_observation(emb.read())


class PolicyRunner:
    """Closed-loop chunk execution: act -> write -> step, re-observe between chunks."""

    def __init__(self, emb: Any, provider: BasePolicyProvider) -> None:
        n_joints = len(emb.spec().joints)
        m = provider.meta
        if m.action_dim != n_joints + 1:
            raise ValueError(
                f"policy action_dim {m.action_dim} != joints+gripper {n_joints + 1} "
                f"({provider.meta.source or 'unknown artifact'})"
            )
        self.emb = emb
        self.provider = provider

    def observe(self) -> tuple[np.ndarray, np.ndarray]:
        obs = self.emb.read()
        state = np.asarray([*obs.qpos, obs.gripper_opening], dtype=np.float32)
        if obs.objects:
            env = np.concatenate(
                [np.asarray([*obs.objects[n].pos, *obs.objects[n].quat]) for n in sorted(obs.objects)]
            ).astype(np.float32)
        else:
            env = np.zeros(0, dtype=np.float32)
        return state, env

    def run_sync(self, *, done: Callable[[], bool], max_seconds: float) -> tuple[bool, str]:
        """Execute chunks until ``done()`` (checked between chunks) or the horizon.

        Returns (ok, reason); a guard-denied write aborts immediately — the guard
        has latched and fighting it is never correct (same rule as motions.py).
        """
        m = self.provider.meta
        per = max(1, round((1.0 / m.control_hz) / self.emb.timestep))
        budget = int(max_seconds * m.control_hz)
        used = 0
        self.provider.reset()
        while used < budget:
            state, env = self.observe()
            if env.shape[0] != m.env_dim:
                return False, f"environment_state dim {env.shape[0]} != policy expects {m.env_dim}"
            chunk = self.provider.act(state, env)
            for row in chunk:
                if used >= budget:
                    break
                result = self.emb.write(
                    ActionCommand(joint_targets=tuple(float(v) for v in row[:-1]), gripper=float(row[-1]))
                )
                if not result.applied:
                    return False, f"guard denied write: {result.reason}"
                self.emb.step(per)
                used += 1
            if done():
                return True, "done"
        return (done(), "horizon reached")


def _pick_policy_sync(emb: Any, provider: BasePolicyProvider, obj: str) -> SkillResult:
    before = _snap(emb)
    if obj not in before.objects:
        return SkillResult(ok=False, detail=f"unknown object {obj!r}")
    if held_object(before) is not None:
        return SkillResult(ok=False, detail=f"gripper already holding {held_object(before)}")
    runner = PolicyRunner(emb, provider)
    ok, reason = runner.run_sync(done=lambda: gripper_holding(_snap(emb), obj), max_seconds=MAX_PICK_SECONDS)
    if not ok:
        return SkillResult(ok=False, detail=f"policy pick failed ({reason})", data={"object": obj})
    return SkillResult(ok=True, detail=f"{obj} grasped by policy", data={"object": obj})


def _place_policy_sync(emb: Any, provider: BasePolicyProvider, region: str) -> SkillResult:
    before = _snap(emb)
    if region not in before.regions:
        return SkillResult(ok=False, detail=f"unknown region {region!r}")
    held = held_object(before)
    if held is None:
        return SkillResult(ok=False, detail="nothing grasped")
    runner = PolicyRunner(emb, provider)
    ok, reason = runner.run_sync(
        done=lambda: object_in_region(_snap(emb), held, region, margin=PLACE_MARGIN_M),
        max_seconds=MAX_PLACE_SECONDS,
    )
    emb.step(SETTLE_STEPS)
    after = _snap(emb)
    if not object_in_region(after, held, region, margin=PLACE_MARGIN_M):
        return SkillResult(ok=False, detail=f"policy place failed ({reason})", data={"object": held})
    return SkillResult(ok=True, detail=f"{held} placed in {region} by policy", data={"object": held, "region": region})


def register_policy_sim_skills(
    registry: SkillRegistry,
    emb: Any,
    *,
    pick: BasePolicyProvider | None = None,
    place: BasePolicyProvider | None = None,
) -> None:
    """Register learned implementations under the scripted skills' manifests.

    Callers must leave the matching names out of ``register_sim_skills`` (its
    ``skip`` parameter) — the registry rejects duplicates by design.
    """
    if pick is not None:
        PolicyRunner(emb, pick)  # validate dims at registration, not first call

        async def pick_handler(object: str = "obj_cube") -> SkillResult:
            return await asyncio.to_thread(_pick_policy_sync, emb, pick, object)

        registry.register(PICK, pick_handler)
    if place is not None:
        PolicyRunner(emb, place)

        async def place_handler(region: str = "bin_region") -> SkillResult:
            return await asyncio.to_thread(_place_policy_sync, emb, place, region)

        registry.register(PLACE, place_handler)
