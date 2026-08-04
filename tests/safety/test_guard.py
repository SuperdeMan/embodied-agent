"""Guard v0 contract tests: pure logic, no sim required. These pin SG-2 semantics —
fix code, never tests, if they regress (docs/architecture.md §4.5)."""

from __future__ import annotations

import pytest

from embodied.control.hal import ActionCommand, Observation, Pose
from embodied.safety.guard import Guard, GuardEvent, GuardLimits

LIMITS = GuardLimits(
    joint_lower=(-1.0, -1.0),
    joint_upper=(1.0, 1.0),
    max_delta_per_write=0.1,
    ee_fence_min=(-0.5, -0.5, -0.005),
    ee_fence_max=(0.5, 0.5, 0.6),
)


def obs(ee=(0.1, 0.1, 0.1), qpos=(0.0, 0.0)) -> Observation:
    return Observation(t=0.0, qpos=qpos, qvel=(0.0, 0.0), gripper_opening=0.5, ee_pose=Pose(pos=ee))


def cmd(targets=(0.05, 0.05), gripper=None, source="skill") -> ActionCommand:
    return ActionCommand(joint_targets=targets, gripper=gripper, source=source)


@pytest.fixture
def guard() -> Guard:
    return Guard(LIMITS)


def test_pass_through(guard):
    applied, result = guard.gate(cmd(), obs())
    assert result.applied and not result.clamped
    assert applied.joint_targets == (0.05, 0.05)


def test_range_clamp(guard):
    applied, result = guard.gate(cmd(targets=(5.0, -5.0), gripper=2.0), obs(qpos=(0.95, -0.95)))
    assert result.applied and result.clamped
    assert applied.joint_targets == (1.0, -1.0)  # range-clamped, within rate window of current qpos
    assert applied.gripper == 1.0


def test_rate_clamp(guard):
    applied, result = guard.gate(cmd(targets=(0.5, 0.0)), obs(qpos=(0.0, 0.0)))
    assert result.clamped
    assert applied.joint_targets[0] == pytest.approx(0.1)  # limited to max_delta_per_write


def test_source_whitelist(guard):
    applied, result = guard.gate(cmd(source="llm_direct"), obs())
    assert applied is None and not result.applied
    assert "source_not_whitelisted" in result.reason


def test_bad_shape(guard):
    applied, result = guard.gate(cmd(targets=(0.1,)), obs())
    assert applied is None and "bad_command_shape" in result.reason


def test_fence_breach_latches(guard):
    applied, result = guard.gate(cmd(), obs(ee=(0.7, 0.0, 0.1)))
    assert applied is None and result.reason == "ee_fence_breach"
    # latched: even a safe follow-up command is refused until reset
    applied2, result2 = guard.gate(cmd(), obs())
    assert applied2 is None and result2.reason.startswith("halted:")
    guard.reset()
    applied3, _ = guard.gate(cmd(), obs())
    assert applied3 is not None


def test_estop_latches(guard):
    guard.estop("test")
    applied, result = guard.gate(cmd(), obs())
    assert applied is None and result.reason.startswith("halted:")


def test_check_target(guard):
    assert guard.check_target((0.2, 0.2, 0.1))
    assert not guard.check_target((0.2, 0.2, 0.7))


def test_events_and_listener_faults_do_not_leak(guard):
    events: list[GuardEvent] = []
    guard.set_listener(lambda e: events.append(e))
    guard.gate(cmd(source="nope"), obs())
    assert events and events[-1].kind == "denied"

    def boom(e: GuardEvent) -> None:
        raise RuntimeError("listener bug")

    guard.set_listener(boom)
    applied, result = guard.gate(cmd(), obs())  # listener fault must not break the gate
    assert result.applied
