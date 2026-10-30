"""Freshness and ordering boundaries for remote state. 远端状态时效与顺序边界。"""

import json

import pytest
from pydantic import ValidationError

from embodied.contracts.observation import ObservationEnvelope
from embodied.runtime.world_state import SnapshotAdmission


def envelope(**changes):
    values = dict(
        robot_id="sim", boot_id="boot", epoch="scene", sequence=1, capture_timestamp=1.,
        capture_duration_s=0.01, calibration_version="v1", qpos=[0.], qvel=[0.],
        ee_pose={"pos": (0., 0., 0.1), "quat": (1., 0., 0., 0.)}, gripper_opening=0.,
        objects={"cube": {"pos": (0., 0., 0.1), "quat": (1., 0., 0., 0.)}},
        regions={"bin": {"center": (0., 0., 0.), "half": (0.1, 0.1, 0.1)}},
    )
    return ObservationEnvelope(**(values | changes))


@pytest.mark.parametrize("changes", [
    {"api_version": "future"}, {"capture_timestamp": float("nan")}, {"capture_duration_s": -1.},
    {"sequence": True}, {"qpos": [float("inf")]}, {"qvel": []}, {"gripper_opening": 2.},
    {"frame_id": "unknown"}, {"source": "device_feedback"}, {"qpos": ["0"]},
])
def test_invalid_snapshot_is_not_a_world_state(changes):
    with pytest.raises(ValidationError):
        envelope(**changes)


def test_json_roundtrip_and_freshness_budget():
    original = envelope()
    parsed = ObservationEnvelope.model_validate_json(original.model_dump_json())
    admission = SnapshotAdmission(max_age_s=0.5)
    assert admission.accept(parsed, 0.49) is not None
    assert admission.accept(envelope(sequence=2), 0.5) is None
    assert admission.accept(envelope(sequence=2), float("nan")) is None


def test_epoch_change_does_not_allow_old_clock_or_reply_to_return():
    admission = SnapshotAdmission()
    assert admission.accept(envelope(), 0.1, 1.) is not None
    assert admission.accept(envelope(sequence=2, capture_timestamp=0.5), 0.1, 2.) is None
    assert admission.accept(envelope(sequence=3, epoch="new", capture_timestamp=0.2), 0.1, 3.) is not None
    assert admission.accept(envelope(sequence=4), 0.1, 4.) is None
    assert admission.accept(envelope(sequence=2, epoch="unseen-old"), 0.1, 4.) is None
    assert admission.accept(envelope(boot_id="reboot", epoch="fresh"), 0.1, 5.) is not None
    assert admission.accept(envelope(boot_id="unseen-old-boot", epoch="old"), 0.1, 2.) is None


def test_calibration_or_robot_change_requires_an_explicit_boundary():
    admission = SnapshotAdmission()
    assert admission.accept(envelope(), 0.) is not None
    assert admission.accept(envelope(sequence=2, calibration_version="changed"), 0.) is None
    assert admission.accept(envelope(sequence=2, robot_id="other"), 0.) is None
    payload = json.loads(envelope().model_dump_json())
    payload["objects"]["cube"]["pos"] = [float("nan"), 0, 0]
    with pytest.raises(ValidationError):
        ObservationEnvelope.model_validate_json(json.dumps(payload))
