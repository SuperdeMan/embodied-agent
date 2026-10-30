"""Reject false or mismatched stop evidence. 拒绝错误或不匹配的停止证据。"""

import time

import pytest
from pydantic import ValidationError

from embodied.contracts.stopping import (
    StopEvidence,
    StopProfile,
    StopRejected,
    StopRequest,
    StopSample,
    validate_stop_evidence,
)


def sample_bundle():
    profile = StopProfile(robot_id="sim", asset_digest="a" * 64, joint_names=("arm", "jaw"),
                          joint_lower=(-1., 0.), joint_upper=(1., 1.))
    request = StopRequest(operation_id="op", attempt_id="attempt", attempt_boot_id="old",
                          observer_boot_id="new", robot_id="sim", lease_epoch=1,
                          resources=profile.resources, nonce="nonce", deadline_monotonic=time.monotonic() + 1)
    evidence = StopEvidence(**request.model_dump(), profile_digest=profile.digest, scene_epoch="scene",
                            observed_at_unix_s=time.time(), measured_at_monotonic=time.monotonic(),
                            hold_targets=(0., 0.5), initial_positions=(0., 0.5), previous_gripper_target=0.5,
                            samples=(StopSample(sim_t=1., positions=(0., 0.5), velocities=(0., 0.01)),
                                     StopSample(sim_t=1.1, positions=(0., 0.5), velocities=(0., 0.01))))
    return profile, request, evidence


@pytest.mark.parametrize("changes", [
    {"attempt_id": "different"}, {"robot_id": "different"}, {"lease_epoch": 2},
    {"observer_boot_id": "different"}, {"profile_digest": "b" * 64},
    {"measured_at_monotonic": 0.}, {"hold_targets": (0., 2.)},
])
def test_identity_limits_and_freshness(changes):
    profile, request, evidence = sample_bundle()
    bad = StopEvidence(**(evidence.model_dump() | changes))
    with pytest.raises(StopRejected):
        validate_stop_evidence(bad, request, profile)


@pytest.mark.parametrize("velocities", [(0., 0.03), (0.,), (float("nan"), 0.)])
def test_jaw_motion_missing_feedback_and_nan_are_not_stopped(velocities):
    profile, request, evidence = sample_bundle()
    with pytest.raises((ValidationError, StopRejected)):
        samples = tuple(StopSample(sim_t=s.sim_t, positions=s.positions, velocities=velocities)
                        for s in evidence.samples)
        bad = StopEvidence(**(evidence.model_dump() | {"samples": samples}))
        validate_stop_evidence(bad, request, profile)


def test_non_advancing_or_short_window_cannot_prove_stop():
    profile, request, evidence = sample_bundle()
    for end in (1., 1.01):
        bad = StopEvidence(**(evidence.model_dump() | {"samples": (
            evidence.samples[0], StopSample(sim_t=end, positions=(0., 0.5), velocities=(0., 0.)),
        )}))
        with pytest.raises(StopRejected):
            validate_stop_evidence(bad, request, profile)
    with pytest.raises(StopRejected):
        validate_stop_evidence(evidence, request, profile, now=float("nan"))


def test_historical_read_does_not_compare_monotonic_across_boots():
    profile, request, evidence = sample_bundle()
    validate_stop_evidence(evidence, request, profile, now=0., historical=True)
    with pytest.raises(StopRejected):
        validate_stop_evidence(evidence, request, profile, now=0.)


def test_contact_preservation_requires_force_samples_on_both_jaws():
    profile, request, evidence = sample_bundle()
    bad = StopEvidence(**(evidence.model_dump() | {
        "gripper_strategy": "maintain_contact", "contact_objects": ("cube",),
    }))
    with pytest.raises(StopRejected, match="contact_shape"):
        validate_stop_evidence(bad, request, profile)
    samples = tuple(StopSample(sim_t=s.sim_t, positions=s.positions, velocities=s.velocities,
                               grip_forces=(1., 0.)) for s in evidence.samples)
    missing_force = StopEvidence(**(bad.model_dump() | {"samples": samples}))
    with pytest.raises(StopRejected, match="contact_not_preserved"):
        validate_stop_evidence(missing_force, request, profile)
