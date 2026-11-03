"""Exercise action authority against native state. 用原生状态验证动作准入。"""

import asyncio
import json
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("mujoco")

from embodied.control.actions import ActionRejected, write_action  # noqa: E402
from embodied.control.drivers.mujoco_sim import TabletopSim  # noqa: E402
from embodied.control.hal import ActionCommand  # noqa: E402
from embodied.control.simassets import find_menagerie_model  # noqa: E402
from embodied.control.teleop import TeleopSession  # noqa: E402
from embodied.runtime.invocation import CURRENT_INVOCATION, InvocationPermit  # noqa: E402
from embodied.skills.manifest import SkillManifest  # noqa: E402
from embodied.skills.policies.runner import PolicyRunner  # noqa: E402
from embodied.skills.registry import SkillRegistry, SkillResult  # noqa: E402

try:
    find_menagerie_model()
except FileNotFoundError:
    pytest.skip("menagerie assets not fetched", allow_module_level=True)


MANIFEST = SkillManifest(name="skill.test.action", description="action admission probe",
                         resources=["arm", "gripper", "workspace"])


@pytest.fixture
def bound():
    sim = TabletopSim(seed=42)
    runtime = SkillRegistry(robot_id=sim.spec().embodiment_id)
    runtime.configure_execution_controller(sim.action_controller)
    runtime.configure_stop_controller(sim.stop_controller)
    yield sim, runtime
    runtime.close()


def envelope(sim):
    obs = sim.read_control_feedback()
    return sim.prepare_action(ActionCommand(obs.qpos, source="teleop"), obs.reference)


def run(runtime, handler):
    return runtime.invoke_operator(MANIFEST, handler)


def test_bound_driver_requires_real_owner_and_disallows_second_runtime(bound):
    sim, runtime = bound
    before = sim.data.ctrl.copy()
    assert not sim.write(ActionCommand(sim.read().qpos)).applied
    np.testing.assert_array_equal(sim.data.ctrl, before)
    other = SkillRegistry(robot_id=runtime.robot_id)
    try:
        with pytest.raises(ActionRejected, match="already_bound"):
            other.configure_execution_controller(sim.action_controller)
    finally:
        other.close()


@pytest.mark.parametrize("changes", [
    {"robot_id": "other"}, {"controller_boot_id": "other"}, {"control_epoch": "old"},
    {"scene_epoch": "old"}, {"profile_digest": "wrong"}, {"runtime_boot_id": "old"},
    {"operation_id": "other"}, {"attempt_id": "other"}, {"lease_epoch": 999},
    {"resources": ("arm",)}, {"source": "skill"}, {"joint_names": ("wrong",) * 6},
    {"mode": "velocity"}, {"units": "degree"}, {"frame_id": "world"},
    {"gripper_mode": "force"}, {"api_version": "wrong"}, {"sequence": True},
    {"joint_targets": (float("nan"),) * 6}, {"gripper": float("inf")},
])
def test_invalid_envelope_cannot_move_or_report_success(bound, changes):
    sim, runtime = bound
    before_ctrl, before_q, before_t = sim.data.ctrl.copy(), sim.data.qpos.copy(), sim.data.time

    def handler():
        invalid = envelope(sim).model_copy(update=changes)
        assert not sim.write_envelope(invalid).applied
        sim.step(10)
        return SkillResult(ok=True)

    result = run(runtime, handler)
    assert not result.ok and result.invocation_state == "OUTCOME_UNKNOWN"
    assert not runtime.arbiter.idle(MANIFEST.resources)
    np.testing.assert_array_equal(sim.data.ctrl, before_ctrl)
    np.testing.assert_array_equal(sim.data.qpos, before_q)
    assert sim.data.time == before_t


@pytest.mark.parametrize("kind", ["forged", "stale", "future", "expired", "too_long", "missing"])
def test_observation_and_deadline_fail_closed(bound, kind):
    sim, runtime = bound
    now = sim.action_controller._clock()
    sim.action_controller._clock = lambda: now
    before = sim.data.ctrl.copy()

    def handler():
        nonlocal now
        action = envelope(sim)
        if kind == "forged":
            action = action.model_copy(update={"observation": action.observation.model_copy(
                update={"reference_id": "not-issued"})})
        elif kind == "stale":
            now += 2.1
            action = sim.prepare_action(ActionCommand(sim.read().qpos, source="teleop"), action.observation)
        elif kind == "future":
            now -= 0.1
        elif kind == "expired":
            now = action.valid_until_monotonic
        elif kind == "too_long":
            action = action.model_copy(update={"valid_until_monotonic": now + 1.1})
        elif kind == "missing":
            action = action.model_copy(update={"observation": None})
        assert not sim.write_envelope(action).applied
        return SkillResult(ok=True)

    result = run(runtime, handler)
    assert result.invocation_state == "OUTCOME_UNKNOWN"
    np.testing.assert_array_equal(sim.data.ctrl, before)


def test_sequence_replay_revokes_even_after_one_accepted_action(bound):
    sim, runtime = bound

    def handler():
        first, second = envelope(sim), envelope(sim)
        assert sim.write_envelope(second).applied
        sim.step()
        t = sim.data.time
        assert not sim.write_envelope(first).applied
        assert not sim.write_envelope(second).applied
        sim.step(10)
        assert sim.data.time == t
        return SkillResult(ok=True)

    assert run(runtime, handler).invocation_state == "OUTCOME_UNKNOWN"


def test_forged_permit_with_copied_identity_has_no_authority(bound):
    sim, runtime = bound

    def handler():
        action = envelope(sim)
        real = CURRENT_INVOCATION.get()
        forged = InvocationPermit(5)
        for name in ("issuer", "operation_id", "attempt_id", "runtime_boot_id", "lease_epoch",
                     "resources", "scene_epoch", "control_epoch", "source"):
            setattr(forged, name, getattr(real, name))
        token = CURRENT_INVOCATION.set(forged)
        try:
            assert sim.write_envelope(action).reason == "missing_bound_invocation"
        finally:
            CURRENT_INVOCATION.reset(token)
        assert sim.write_envelope(action).applied
        return SkillResult(ok=True)

    assert run(runtime, handler).ok


def test_partial_resource_permit_is_rejected(bound):
    sim, runtime = bound

    def handler():
        action = envelope(sim)
        assert sim.write_envelope(action).reason == "action_resources_missing"
        return SkillResult(ok=True)

    partial = MANIFEST.model_copy(update={"resources": ["arm"]})
    assert runtime.invoke_operator(partial, handler).invocation_state == "OUTCOME_UNKNOWN"


def test_wrong_joint_order_rejected_even_with_correct_shape(bound):
    sim, runtime = bound

    def handler():
        action = envelope(sim)
        invalid = action.model_copy(update={"joint_names": action.joint_names[::-1]})
        assert sim.write_envelope(invalid).reason == "action_identity_or_layout_mismatch"
        return SkillResult(ok=True)

    assert run(runtime, handler).invocation_state == "OUTCOME_UNKNOWN"


@pytest.mark.parametrize("accepted", [False, True])
def test_step_requires_unexpired_accepted_action(bound, accepted):
    sim, runtime = bound
    now = sim.action_controller._clock()
    sim.action_controller._clock = lambda: now

    def handler():
        nonlocal now
        if accepted:
            action = envelope(sim)
            assert sim.write_envelope(action).applied
            now = action.valid_until_monotonic
        t = sim.data.time
        sim.step(10)
        assert sim.data.time == t
        return SkillResult(ok=True)

    assert run(runtime, handler).invocation_state == "OUTCOME_UNKNOWN"


def test_guard_rejection_revokes_prior_target(bound):
    sim, runtime = bound

    def handler():
        action = envelope(sim)
        assert sim.write_envelope(action).applied
        invalid = envelope(sim)
        sim.guard.estop("test_stop")
        assert not sim.write_envelope(invalid).applied
        assert CURRENT_INVOCATION.get().rejection_reason == "halted:test_stop"
        t = sim.data.time
        sim.step(10)
        assert sim.data.time == t
        return SkillResult(ok=True)

    assert run(runtime, handler).invocation_state == "OUTCOME_UNKNOWN"


async def test_active_skill_blocks_teleop_reset_and_idle_step(bound, tmp_path):
    sim, runtime = bound
    entered, release = asyncio.Event(), asyncio.Event()
    session = TeleopSession(sim, task="test", root=tmp_path, registry=runtime)
    session.start_episode(randomize=False)
    writer = session.writer

    async def handler():
        entered.set()
        await release.wait()
        return SkillResult(ok=True)

    runtime.register(MANIFEST, handler)
    task = asyncio.create_task(runtime.invoke(MANIFEST.name))
    await entered.wait()
    try:
        t, ctrl = sim.data.time, sim.data.ctrl.copy()
        assert not session.jog(0, 0, 0.02).ok
        assert session.last_result.invocation_state == "REJECTED"
        with pytest.raises(ActionRejected, match="not_idle"):
            session.start_episode()
        with pytest.raises(ActionRejected, match="not_idle"):
            session.finish_episode(True)
        sim.step(20)
        assert sim.data.time == t and session.writer is writer
        np.testing.assert_array_equal(sim.data.ctrl, ctrl)
    finally:
        release.set()
        assert (await task).ok
        session.close()


def test_quarantine_blocks_reset_and_teleop_until_observed_stop(bound, tmp_path):
    sim, runtime = bound
    result = run(runtime, lambda: SkillResult(ok=False, data={"_execution": {"outcome": "OUTCOME_UNKNOWN"}}))
    session = TeleopSession(sim, task="test", root=tmp_path, record=False, registry=runtime)
    assert not session.home()
    with pytest.raises(ActionRejected, match="not_idle"):
        sim.reset()
    t = sim.data.time
    sim.step(10)
    assert sim.data.time == t
    record = asyncio.run(runtime.reconcile_stop(result.operation_id, result.attempt_id, confirmed=True))
    assert record.state == "OUTCOME_UNKNOWN" and record.stop_state == "MOTION_STOPPED_OBSERVED"
    assert session.home()


def test_return_revokes_worker_context_and_reset_rotates_references(bound):
    sim, runtime = bound
    retained = []

    def handler():
        action = envelope(sim)
        retained.extend([CURRENT_INVOCATION.get(), action])
        assert sim.write_envelope(action).applied
        return SkillResult(ok=True)

    assert run(runtime, handler).ok
    token = CURRENT_INVOCATION.set(retained[0])
    try:
        t = sim.data.time
        assert not sim.write_envelope(retained[1]).applied
        sim.step(10)
        assert sim.data.time == t
    finally:
        CURRENT_INVOCATION.reset(token)
    old = sim.read().reference
    sim.reset()
    new = sim.read().reference
    assert old.scene_epoch != new.scene_epoch and old.control_epoch != new.control_epoch
    assert old.reference_id not in sim.action_controller._references


def test_slow_policy_cannot_refresh_its_input_timestamp(bound):
    sim, runtime = bound
    now = sim.action_controller._clock()
    sim.action_controller._clock = lambda: now
    initial = sim.read()

    class SlowPolicy:
        meta = SimpleNamespace(action_dim=len(initial.qpos) + 1, control_hz=50,
                               env_dim=len(initial.objects) * 7)

        def reset(self):
            pass

        def act(self, state, env):
            nonlocal now
            now += 2.1
            sim.read()
            return np.array([state])

    def handler():
        ok, reason = PolicyRunner(sim, SlowPolicy()).run_sync(done=lambda: False, max_seconds=0.1)
        assert not ok and "control_observation_stale" in reason
        return SkillResult(ok=True)

    t, ctrl = sim.data.time, sim.data.ctrl.copy()
    assert run(runtime, handler).invocation_state == "OUTCOME_UNKNOWN"
    assert sim.data.time == t
    np.testing.assert_array_equal(sim.data.ctrl, ctrl)


def test_missing_producer_reference_cannot_borrow_a_later_read(bound):
    sim, runtime = bound

    def handler():
        obs = sim.read()
        obs.reference = None
        sim.read()
        assert write_action(sim, ActionCommand(obs.qpos), observation=obs).reason == "missing_control_observation"
        return SkillResult(ok=True)

    assert run(runtime, handler).invocation_state == "OUTCOME_UNKNOWN"


def test_teleop_records_operation_identity_without_exposing_model_tools(bound, tmp_path):
    sim, runtime = bound
    session = TeleopSession(sim, task="demo", root=tmp_path, registry=runtime)
    session.start_episode(randomize=False)
    result = session.jog(0, 0, 0.02)
    assert result.ok and result.operation_id and result.attempt_id
    record = runtime.get_operation(result.operation_id)
    assert record.state == "RETURNED" and record.stop_context["execution_source"] == "teleop"
    assert runtime.catalog() == []
    path = session.finish_episode(True)
    events = [json.loads(line) for line in (path / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    authority = [event["payload"] for event in events if event["payload"].get("kind") == "action_authority"]
    assert len(authority) == 1
    assert authority[0]["operation_id"] == result.operation_id
    assert authority[0]["attempt_id"] == result.attempt_id and authority[0]["source"] == "teleop"
