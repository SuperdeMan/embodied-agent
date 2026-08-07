"""Learned-skill runner — hermetic (fake embodiment, mock policy; no onnx/mujoco).

Pins the M2 impl-swap contract: policy skills register under the SAME manifests as
the scripted skills, success comes from ground-truth predicates (never the policy),
and guard vetoes fail the rollout closed.
"""

from __future__ import annotations

import numpy as np
import pytest

from embodied.control.hal import EmbodimentSpec, JointSpec, Observation, Pose, WriteResult
from embodied.providers.policy import MockChunkPolicy, PolicyMeta
from embodied.skills.policies.runner import PolicyRunner, register_policy_sim_skills
from embodied.skills.registry import SkillRegistry
from embodied.skills.scripted.manip import register_sim_skills

META = PolicyMeta(chunk_size=5, control_hz=50.0, state_dim=3, env_dim=7, action_dim=3)

TABLE = (0.2, 0.0, 0.015)
IN_HAND = (0.0, 0.0, 0.1)  # near ee (0,0,0.12), above GRASP_MIN_Z -> held
IN_BIN = (0.16, -0.18, 0.03)


class FakeEmb:
    """Two joints + gripper; object teleports per a scripted write-count schedule."""

    def __init__(self, *, obj=TABLE, deny_after=None, schedule=()):
        self.t = 0.0
        self.q = np.zeros(2)
        self.grip = 1.0
        self.obj = obj
        self.deny_after = deny_after
        self.schedule = list(schedule)  # [(write_count, new_obj_pos)]
        self.writes = 0
        self.steps = 0

    def spec(self):
        return EmbodimentSpec(
            embodiment_id="sim.fake",
            joints=(JointSpec("a", -3, 3), JointSpec("b", -3, 3)),
            gripper_joint=JointSpec("jaw", 0.0, 1.0),
        )

    @property
    def timestep(self):
        return 0.002

    def read(self):
        return Observation(
            t=self.t, qpos=tuple(self.q), qvel=(0.0, 0.0), gripper_opening=self.grip,
            ee_pose=Pose(pos=(0.0, 0.0, 0.12)),
            objects={"obj_cube": Pose(pos=self.obj)},
            extras={"regions": {"bin_region": {"center": (0.16, -0.18, 0.045), "half": (0.045, 0.045, 0.04)}}},
        )

    def write(self, cmd):
        if self.deny_after is not None and self.writes >= self.deny_after:
            return WriteResult(applied=False, reason="fence breach latched")
        self.writes += 1
        self.q = np.asarray(cmd.joint_targets, dtype=float)
        if cmd.gripper is not None:
            self.grip = float(cmd.gripper)
        for count, pos in self.schedule:
            if self.writes >= count:
                self.obj = pos
        return WriteResult(applied=True)

    def step(self, n=1):
        self.steps += n
        self.t += 0.002 * n


def hold_policy():
    return MockChunkPolicy(META, lambda s, e: np.tile(s, (META.chunk_size, 1)))


async def invoke_pick(emb, policy):
    registry = SkillRegistry()
    register_sim_skills(registry, emb, skip=("skill.manip.pick",))
    register_policy_sim_skills(registry, emb, pick=policy)
    return await registry.invoke("skill.manip.pick", {"object": "obj_cube"})


def test_manifests_identical_after_impl_swap():
    scripted, hybrid = SkillRegistry(), SkillRegistry()
    emb = FakeEmb()
    register_sim_skills(scripted, emb)
    register_sim_skills(hybrid, emb, skip=("skill.manip.pick", "skill.manip.place"))
    register_policy_sim_skills(hybrid, emb, pick=hold_policy(), place=hold_policy())
    assert [m.name for m in hybrid.catalog()] == [m.name for m in scripted.catalog()]
    # the exact manifest OBJECTS are shared — verification/require_confirm cannot drift
    assert hybrid.get("skill.manip.pick") is scripted.get("skill.manip.pick")


def test_action_dim_mismatch_rejected_at_registration():
    bad = MockChunkPolicy(
        PolicyMeta(chunk_size=5, control_hz=50.0, state_dim=3, env_dim=7, action_dim=4),
        lambda s, e: np.zeros((5, 4)),
    )
    with pytest.raises(ValueError, match="action_dim"):
        register_policy_sim_skills(SkillRegistry(), FakeEmb(), pick=bad)


async def test_pick_succeeds_when_predicate_turns_true():
    emb = FakeEmb(schedule=[(12, IN_HAND)])
    policy = hold_policy()
    result = await invoke_pick(emb, policy)
    assert result.ok and result.data == {"object": "obj_cube"}
    # done() is checked between chunks: 12 writes -> caught at the 3rd chunk boundary
    assert policy.resets == 1 and policy.calls == 3 and emb.writes == 15
    # 50 Hz on a 2 ms sim -> 10 physics steps per write
    assert emb.steps == emb.writes * 10


async def test_pick_fails_closed_on_guard_veto():
    result = await invoke_pick(FakeEmb(deny_after=7), hold_policy())
    assert not result.ok and "guard denied write" in result.detail


async def test_pick_horizon_reached_reports_failure():
    emb = FakeEmb()  # object never moves
    result = await invoke_pick(emb, hold_policy())
    assert not result.ok and "horizon reached" in result.detail
    assert emb.writes == int(12.0 * 50)  # MAX_PICK_SECONDS budget, then stop


async def test_pick_refuses_when_already_holding():
    result = await invoke_pick(FakeEmb(obj=IN_HAND), hold_policy())
    assert not result.ok and "already holding" in result.detail


async def test_env_dim_mismatch_fails_with_reason():
    wide = MockChunkPolicy(
        PolicyMeta(chunk_size=5, control_hz=50.0, state_dim=3, env_dim=14, action_dim=3),
        lambda s, e: np.zeros((5, 3)),
    )
    result = await invoke_pick(FakeEmb(), wide)
    assert not result.ok and "environment_state dim" in result.detail


async def test_place_judged_by_region_ground_truth():
    emb = FakeEmb(obj=IN_HAND, schedule=[(8, IN_BIN)])
    registry = SkillRegistry()
    register_sim_skills(registry, emb, skip=("skill.manip.place",))
    register_policy_sim_skills(registry, emb, place=hold_policy())
    result = await registry.invoke("skill.manip.place", {"region": "bin_region"})
    assert result.ok and result.data == {"object": "obj_cube", "region": "bin_region"}


async def test_place_needs_something_grasped():
    emb = FakeEmb(obj=TABLE)
    registry = SkillRegistry()
    register_sim_skills(registry, emb, skip=("skill.manip.place",))
    register_policy_sim_skills(registry, emb, place=hold_policy())
    result = await registry.invoke("skill.manip.place", {"region": "bin_region"})
    assert not result.ok and "nothing grasped" in result.detail


def test_runner_observe_layout_matches_converter():
    emb = FakeEmb(obj=TABLE)
    runner = PolicyRunner(emb, hold_policy())
    state, env = runner.observe()
    assert np.allclose(state, [0.0, 0.0, 1.0])  # qpos + gripper_opening
    assert env.shape == (7,) and env.dtype == np.float32
    assert np.allclose(env[:3], TABLE) and np.allclose(env[3:], [1.0, 0.0, 0.0, 0.0])
