"""Numerical command, observation and trusted configuration admission."""

from dataclasses import replace

import pytest

from embodied.control.hal import ActionCommand, Observation, Pose
from embodied.safety.guard import Guard, GuardLimits


def setup():
    return Guard(GuardLimits(joint_lower=(-1.,), joint_upper=(1.,))), Observation(
        t=0., qpos=(0.,), qvel=(0.,), gripper_opening=0.5, ee_pose=Pose(pos=(0., 0., 0.1)),
    )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), True, "0.1"])
@pytest.mark.parametrize("field", ["joint_targets", "gripper"])
def test_bad_commands_are_rejected(value, field):
    guard, obs = setup()
    command = ActionCommand(joint_targets=(0.,))
    setattr(command, field, (value,) if field == "joint_targets" else value)
    applied, result = guard.gate(command, obs)
    assert applied is None and not result.applied
    assert result.reason == "invalid_command_value"


@pytest.mark.parametrize("changes", [
    {"qpos": ()}, {"qvel": ()}, {"qpos": (float("nan"),)},
    {"qvel": (float("inf"),)}, {"t": float("nan")},
    {"ee_pose": Pose(pos=(float("nan"), 0., 0.1))},
])
def test_bad_observation_latches(changes):
    guard, obs = setup()
    applied, result = guard.gate(ActionCommand(joint_targets=(0.,)), replace(obs, **changes))
    assert applied is None and not result.applied and guard.halted
    assert not guard.gate(ActionCommand(joint_targets=(0.,)), obs)[1].applied


def test_rate_clamp_cannot_push_outside_joint_limits():
    guard, obs = setup()
    applied, result = guard.gate(ActionCommand(joint_targets=(1.,)), replace(obs, qpos=(1.5,)))
    assert applied is None and not result.applied


@pytest.mark.parametrize("changes", [
    {"joint_upper": ()}, {"joint_upper": (float("nan"),)},
    {"joint_upper": (-2.,)}, {"max_delta_per_write": 0},
    {"ee_fence_min": (0.,)}, {"ee_fence_max": (float("inf"), 1., 1.)},
])
def test_invalid_trusted_limits_fail_at_startup(changes):
    values = {"joint_lower": (-1.,), "joint_upper": (1.,)} | changes
    with pytest.raises(ValueError):
        GuardLimits(**values)
