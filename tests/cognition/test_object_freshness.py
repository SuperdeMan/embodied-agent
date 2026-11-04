"""Object lifetime faults without a renderer. 不依赖渲染器的对象时效故障测试。"""

import time
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from embodied.cognition.executor import DagExecutor
from embodied.cognition.perception import ObjectSpec, PerceivedSim
from embodied.cognition.plan import Plan, PlanContext, Step, StepStatus
from embodied.cognition.verify import UNKNOWN, eval_state_predicate
from embodied.cognition.world_state import WorldSnapshot, gripper_holding, object_in_region
from embodied.contracts.object_state import ObjectContext, ObjectEvidence, object_available
from embodied.control.camera import CameraModel
from embodied.control.hal import Observation, Pose
from embodied.skills.manifest import SkillManifest
from embodied.skills.registry import SkillRegistry, SkillResult


class Body:
    def __init__(self):
        self.t = 0.0
        self.epoch = "scene1"
        self.ee = (0.0, 0.0, 0.1)
        self.grip = 0.7
        self.cam = CameraModel(10, 10, 10.0, 10.0, 5.0, 5.0, (0.0, 0.0, 1.0),
                               (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0))
        self.fail_camera = False

    def read(self):
        return Observation(
            t=self.t, qpos=(0.0,), qvel=(0.0,), gripper_opening=self.grip, ee_pose=Pose(self.ee),
            objects={"cube": Pose((9.0, 9.0, 9.0))},
            extras={"regions": {"bin": {"center": (0.0, 0.0, 0.1), "half": (0.1, 0.1, 0.1)}}},
            reference=SimpleNamespace(controller_boot_id="boot", control_epoch="control", scene_epoch=self.epoch),
        )

    def reset(self, **kwargs):
        self.epoch += "x"
        self.t = 0.0
        return self.read()

    def camera_model(self, *args, **kwargs):
        return self.cam

    def render_rgbd(self, *args):
        if self.fail_camera:
            raise OSError("camera disconnected")
        return np.zeros((10, 10, 3)), np.ones((10, 10))


class Pipeline:
    objects = {"cube": ObjectSpec("cube")}

    def __init__(self):
        self.visible = True
        self.hook = None
        self.calls = 0

    def locate(self, *args, **kwargs):
        self.calls += 1
        if self.hook is not None:
            self.hook()
        return {"cube": Pose((0.0, 0.0, 0.1))} if self.visible else {}


@pytest.fixture
def scene():
    body, pipeline = Body(), Pipeline()
    wrapped = PerceivedSim(body, pipeline, cameras=("camera",))
    clock = [time.monotonic()]
    wrapped._clock = lambda: clock[0]
    wrapped._render_rgbd = body.render_rgbd
    return body, pipeline, wrapped, clock


@pytest.mark.parametrize("clock_kind", ["simulation", "wall"])
def test_missed_detection_expires_without_read_refresh(scene, clock_kind):
    body, pipeline, wrapped, clock = scene
    first = wrapped.read()
    pipeline.visible = False
    body.t = 0.4
    again = wrapped.read()
    assert again.object_evidence["cube"] == first.object_evidence["cube"]
    assert "cube" in again.objects
    if clock_kind == "simulation":
        body.t = 2.0
    else:
        clock[0] += 2.0
    expired = wrapped.read()
    assert "cube" not in expired.objects
    assert expired.object_evidence["cube"].captured_monotonic == first.object_evidence["cube"].captured_monotonic
    snap = WorldSnapshot.from_observation(expired)
    assert eval_state_predicate({"predicate": "object_in_region", "args": {"object": "cube", "region": "bin"}}, snap) == UNKNOWN
    assert wrapped.read_truth().objects["cube"].pos == (9.0, 9.0, 9.0)


def test_kinematic_estimate_keeps_original_detection_deadline(scene):
    body, pipeline, wrapped, clock = scene
    body.grip = 0.0
    first = wrapped.read()
    pipeline.visible = False
    body.ee, body.t = (0.1, 0.0, 0.1), 0.4
    estimate = wrapped.read()
    assert estimate.objects["cube"].pos == body.ee
    evidence = estimate.object_evidence["cube"]
    assert evidence.source == "kinematic"
    assert evidence.captured_sim_t == first.object_evidence["cube"].captured_sim_t
    assert evidence.sequence == first.object_evidence["cube"].sequence
    body.t = 2.0
    assert "cube" not in wrapped.read().objects
    assert wrapped._attached is None


@pytest.mark.parametrize("change", ["epoch", "calibration", "sim_rollback", "wall_rollback"])
def test_identity_changes_discard_cache_and_retained_policy_input(scene, change):
    body, pipeline, wrapped, clock = scene
    body.t = 1.0
    first = wrapped.read()
    pipeline.visible = False
    if change == "epoch":
        body.epoch = "new"
    elif change == "calibration":
        body.cam = replace(body.cam, fx=20.0)
    elif change == "sim_rollback":
        body.t = 0.5
    else:
        clock[0] -= 0.5
    assert "cube" not in wrapped.read().objects
    assert not wrapped.validate_object_observation(first)


@pytest.mark.parametrize("change", ["epoch", "calibration", "slow", "bad_pose"])
def test_inference_result_cannot_cross_identity_or_expiry(scene, change):
    body, pipeline, wrapped, clock = scene

    def mutate():
        if change == "epoch":
            body.epoch = "new"
        elif change == "calibration":
            body.cam = replace(body.cam, fx=20.0)
        elif change == "slow":
            clock[0] += 2.1

    pipeline.hook = mutate
    if change == "bad_pose":
        pipeline.locate = lambda *args, **kwargs: {"cube": Pose((float("nan"), 0.0, 0.1))}
    assert "cube" not in wrapped.read().objects


def test_camera_failure_does_not_renew_and_recovery_needs_real_detection(scene):
    body, pipeline, wrapped, clock = scene
    first = wrapped.read()
    body.fail_camera = True
    body.t = 0.4
    cached = wrapped.read()
    assert "cube" in cached.objects and wrapped.last_error == "OSError"
    assert cached.object_evidence == first.object_evidence
    body.t = 2.1
    assert "cube" not in wrapped.read().objects
    body.fail_camera = False
    clock[0] += 0.3
    recovered = wrapped.read()
    assert recovered.object_evidence["cube"].sequence > first.object_evidence["cube"].sequence
    assert "cube" in recovered.objects


def test_slow_camera_does_not_get_a_completion_timestamp(scene):
    body, pipeline, wrapped, clock = scene
    initial = clock[0]
    pipeline.hook = lambda: clock.__setitem__(0, initial + 0.5)
    obs = wrapped.read()
    assert obs.object_evidence["cube"].captured_monotonic == initial
    assert wrapped.validate_object_observation(obs)
    clock[0] = initial + 2.0
    assert not wrapped.validate_object_observation(obs)


@pytest.mark.parametrize("fault", ["missing", "expired", "future", "wrong_scene", "wrong_clock", "wrong_calibration"])
def test_all_spatial_predicates_treat_bad_evidence_as_unknown(scene, fault):
    _, _, wrapped, _ = scene
    snap = WorldSnapshot.from_observation(wrapped.read())
    item = snap.object_evidence["cube"]
    if fault == "missing":
        snap.object_evidence.clear()
    elif fault == "expired":
        snap.object_evidence["cube"] = item.model_copy(update={"captured_monotonic": time.monotonic() - 3})
    elif fault == "future":
        snap.object_evidence["cube"] = item.model_copy(update={"captured_monotonic": time.monotonic() + 3})
    else:
        key = {"wrong_scene": "scene_epoch", "wrong_clock": "clock_id", "wrong_calibration": "calibration_id"}[fault]
        snap.object_evidence["cube"] = item.model_copy(update={key: "other"})
    assert not object_available(snap, "cube")
    assert not object_in_region(snap, "cube", "bin") and not gripper_holding(snap, "cube")
    for name, args in [("object_in_region", {"region": "bin"}), ("object_near", {"pos": [0, 0, 0.1]}),
                       ("gripper_holding", {})]:
        assert eval_state_predicate({"predicate": name, "args": {"object": "cube", **args}}, snap) == UNKNOWN


@pytest.mark.parametrize("budget", [0, -1, float("nan"), float("inf")])
def test_invalid_ttl_rejected_before_capture(budget):
    with pytest.raises(ValueError):
        PerceivedSim(Body(), Pipeline(), object_ttl_wall_s=budget)


def test_evidence_contract_rejects_nonfinite_time():
    context = ObjectContext(clock_id="c", scene_epoch="s", calibration_id="p")
    with pytest.raises(ValueError):
        ObjectEvidence(**context.model_dump(), sensor_id="camera", sequence=1, captured_sim_t=0.0,
                       captured_monotonic=float("nan"), ttl_sim_s=2.0, ttl_wall_s=2.0)


def test_retained_snapshot_expires_without_a_new_read(scene, monkeypatch):
    _, _, wrapped, clock = scene
    snap = WorldSnapshot.from_observation(wrapped.read())
    assert object_in_region(snap, "cube", "bin")
    monkeypatch.setattr("embodied.contracts.object_state.time.monotonic", lambda: clock[0] + 2.0)
    assert not object_in_region(snap, "cube", "bin")


async def test_expired_effect_blocks_dependent_handler(scene):
    body, pipeline, wrapped, _ = scene
    wrapped.read()
    pipeline.visible = False
    body.t = 2.1
    snapshot = WorldSnapshot.from_observation(wrapped.read())
    calls = []

    async def claim():
        calls.append("claim")
        return SkillResult(ok=True)

    async def dependent():
        calls.append("dependent")
        return SkillResult(ok=True)

    registry = SkillRegistry()
    verification = {"mode": "state_predicate", "timeout_ms": 1,
                    "expect": {"predicate": "object_in_region", "args": {"object": "cube", "region": "bin"}}}
    registry.register(SkillManifest(name="skill.test.claim", description="claim", verification=verification), claim)
    registry.register(SkillManifest(name="skill.test.next", description="next"), dependent)
    plan = Plan(steps=[Step(id="a", skill="skill.test.claim", verification=verification),
                       Step(id="b", skill="skill.test.next", depends_on=["a"])])
    try:
        results = [result async for result in DagExecutor(registry, world_fn=lambda: snapshot).run(plan, PlanContext())]
        assert results[0].data["_verify"]["verdict"] == UNKNOWN
        assert results[1].status == StepStatus.SKIPPED and calls == ["claim"]
    finally:
        registry.close()
