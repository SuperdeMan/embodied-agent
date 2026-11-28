"""Cross-host observation clock-domain, skew/drift, and per-object TTL rebase tests (D036).

跨主机观察时钟域、偏斜/漂移/Cristian 中点反查与逐对象本地重基准契约测试（D036）。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from embodied.cognition.verify import SAT, UNKNOWN, eval_state_predicate
from embodied.contracts.object_state import object_available
from embodied.contracts.observation import (
    ClockAdmissionPolicy,
    ClockMetadata,
    ObservationEnvelope,
    RemoteObjectEvidence,
)
from embodied.runtime.world_state import SnapshotAdmission


def _remote_ev(**changes) -> RemoteObjectEvidence:
    base = dict(
        source="vision",
        sensor_id="top",
        sequence=1,
        scene_epoch="boot:ctrl:scene1",
        calibration_id="cal-sha-1",
        captured_sim_t=1.0,
        age_sim_s=0.0,
        age_wall_s=0.1,
        ttl_sim_s=2.0,
        ttl_wall_s=1.0,
    )
    return RemoteObjectEvidence(**(base | changes))


def _envelope(**changes) -> ObservationEnvelope:
    base = dict(
        robot_id="sim.mujoco.so_arm100",
        boot_id="boot-1",
        epoch="scene-1",
        sequence=1,
        clock_domain="simulation",
        capture_timestamp=1.0,
        capture_duration_s=0.01,
        calibration_version="cal-sha-1",
        source="perceived_sim",
        qpos=[0.0],
        qvel=[0.0],
        ee_pose={"pos": (0.0, 0.0, 0.1), "quat": (1.0, 0.0, 0.0, 0.0)},
        gripper_opening=0.2,
        objects={"obj_cube": {"pos": (0.0, 0.0, 0.1), "quat": (1.0, 0.0, 0.0, 0.0)}},
        regions={"bin_region": {"center": (0.0, 0.0, 0.1), "half": (0.1, 0.1, 0.1)}},
        clock=ClockMetadata(
            clock_domain="simulation",
            clock_id="clk-sim-1",
            host_id="host-control",
            measurement_source="simulation_clock",
        ),
        object_evidence={"obj_cube": _remote_ev()},
        object_calibration_id="cal-sha-1",
        object_scene_epoch="boot:ctrl:scene1",
    )
    return ObservationEnvelope(**(base | changes))


@pytest.mark.parametrize(
    "changes",
    [
        # 非 simulation 时钟域必须显式提供 clock 元数据。 / Non-simulation clock_domain requires explicit ClockMetadata.
        {"clock_domain": "rpc_rtt_bounded", "clock": None},
        # 包络与 ClockMetadata 的 clock_domain 必须一致。 / Envelope and ClockMetadata clock_domain must agree.
        {
            "clock_domain": "simulation",
            "clock": ClockMetadata(
                clock_domain="rpc_rtt_bounded",
                clock_id="clk-1",
                host_id="host-a",
                measurement_source="rpc_rtt_probe",
            ),
        },
        # sim_truth 不得混带感知对象证据。 / sim_truth must never carry semantic object_evidence.
        {"source": "sim_truth"},
        # 感知包络缺失 object_evidence 或缺失某对象证据必须拒绝。 / Perceived envelope requires evidence for every listed object.
        {"object_evidence": None},
        {"object_evidence": {}},
        # 标定或场景代次不一致必须拒绝。 / Calibration or scene_epoch mismatch must fail closed.
        {"object_evidence": {"obj_cube": _remote_ev(calibration_id="other-cal")}},
        {"object_evidence": {"obj_cube": _remote_ev(scene_epoch="other-scene")}},
        # 对象采集仿真时刻不得晚于包络时刻。 / Object capture time cannot be in the future of the envelope.
        {"object_evidence": {"obj_cube": _remote_ev(captured_sim_t=1.5)}},
    ],
)
def test_envelope_clock_and_object_evidence_contract_invariants(changes):
    """Contract validator rejects mismatched clock domains, mixed sources, or missing object evidence.

    契约校验器必须拒绝时钟域错配、真值与感知混用、缺失对象证据及未来采集时刻。
    """
    with pytest.raises(ValidationError):
        _envelope(**changes)


@pytest.mark.parametrize(
    "kwargs",
    [
        # measurement_source 与 clock_domain 不匹配。 / Mismatched measurement_source.
        {"clock_domain": "wall_ntp", "clock_id": "c", "host_id": "h", "sync_status": "synchronized",
         "source_unix_s": 1_700_000_000.0, "measurement_source": "simulation_clock"},
        # wall_ntp 缺少 source_unix_s。 / wall_ntp missing source_unix_s.
        {"clock_domain": "wall_ntp", "clock_id": "c", "host_id": "h", "sync_status": "synchronized",
         "measurement_source": "ntp_declared"},
        # wall_ptp sync_status 为 not_applicable。 / wall_ptp with not_applicable sync_status.
        {"clock_domain": "wall_ptp", "clock_id": "c", "host_id": "h", "sync_status": "not_applicable",
         "source_unix_s": 1_700_000_000.0, "measurement_source": "ptp_declared"},
        # publish_unix_s 早于 source_unix_s。 / publish_unix_s earlier than source_unix_s.
        {"clock_domain": "wall_ntp", "clock_id": "c", "host_id": "h", "sync_status": "synchronized",
         "source_unix_s": 1_700_000_010.0, "publish_unix_s": 1_700_000_009.0, "measurement_source": "ntp_declared"},
    ],
)
def test_clock_metadata_rejects_inconsistent_fields(kwargs):
    """ClockMetadata rejects inconsistent measurement_source, missing wall timestamp, or inverted publish time.

    ClockMetadata 拒绝来源错配、缺失墙钟时间或发布早于采集的非法元数据。
    """
    with pytest.raises(ValidationError):
        ClockMetadata(**kwargs)


def test_cross_host_local_monotonic_forbidden_but_same_host_allowed():
    """Cross-host local_monotonic is rejected fail-closed; same host_id is accepted.

    跨主机 local_monotonic 一律拒收，同 host_id 才允许接受。
    """
    policy = ClockAdmissionPolicy(consumer_host_id="host-agent", max_age_s=1.0)
    admission = SnapshotAdmission(policy=policy)

    remote_mono = _envelope(
        clock_domain="local_monotonic",
        clock=ClockMetadata(
            clock_domain="local_monotonic",
            clock_id="mono-control",
            host_id="host-control",
            measurement_source="local_os_monotonic",
        ),
    )
    assert admission.accept(remote_mono, 0.02, 1.0, received_monotonic=10.0, received_unix_s=1_700_000_000.0) is None
    assert admission.last_rejection_reason == "cross_host_monotonic_forbidden"

    same_host_mono = _envelope(
        sequence=2,
        clock_domain="local_monotonic",
        clock=ClockMetadata(
            clock_domain="local_monotonic",
            clock_id="mono-agent",
            host_id="host-agent",
            measurement_source="local_os_monotonic",
        ),
    )
    snap = admission.accept(same_host_mono, 0.02, 2.0, received_monotonic=10.5, received_unix_s=1_700_000_000.5)
    assert snap is not None
    assert "obj_cube" in snap.objects


def test_wall_clock_skew_drift_sync_future_and_cristian_midpoint_checks():
    """Synchronized wall-clock domains enforce sync_status, skew/drift budgets, future bound, and Cristian midpoint check.

    同步墙钟域强制校验同步状态、偏斜与漂移预算、未来时间戳拒止及 Cristian 中点偏斜反查。
    """
    policy = ClockAdmissionPolicy(
        consumer_host_id="host-agent",
        max_age_s=1.0,
        max_allowed_skew_s=0.05,
        max_allowed_drift_ppm=200.0,
    )
    rx_unix = 1_700_000_100.0
    rx_mono = 50.0

    def wall_env(seq: int, **clock_overrides) -> ObservationEnvelope:
        base_clock = dict(
            clock_domain="wall_ntp",
            clock_id="ntp-control-1",
            host_id="host-control",
            sync_status="synchronized",
            max_clock_skew_s=0.02,
            clock_drift_ppm=50.0,
            source_unix_s=rx_unix - 0.05,
            publish_unix_s=rx_unix - 0.01,
            measurement_source="ntp_declared",
        )
        return _envelope(
            sequence=seq,
            clock_domain=base_clock["clock_domain"],
            clock=ClockMetadata(**(base_clock | clock_overrides)),
            object_evidence={"obj_cube": _remote_ev(age_wall_s=0.04, captured_unix_s=rx_unix - 0.05)},
        )

    admission = SnapshotAdmission(policy=policy)

    # 1. 未同步或降级状态拒绝。 / Unsynchronized or degraded clock rejected.
    assert admission.accept(wall_env(1, sync_status="unsynchronized"), 0.02, 1.0,
                            received_monotonic=rx_mono, received_unix_s=rx_unix) is None
    assert admission.last_rejection_reason == "clock_not_synchronized:unsynchronized"
    assert admission.accept(wall_env(2, sync_status="degraded"), 0.02, 1.0,
                            received_monotonic=rx_mono, received_unix_s=rx_unix) is None
    assert admission.last_rejection_reason == "clock_not_synchronized:degraded"

    # 2. 超偏斜或漂移预算拒绝。 / Exceeding skew or drift budget rejected.
    assert admission.accept(wall_env(3, max_clock_skew_s=0.08), 0.02, 1.0,
                            received_monotonic=rx_mono, received_unix_s=rx_unix) is None
    assert admission.last_rejection_reason == "clock_skew_budget_exceeded"
    assert admission.accept(wall_env(4, clock_drift_ppm=400.0), 0.02, 1.0,
                            received_monotonic=rx_mono, received_unix_s=rx_unix) is None
    assert admission.last_rejection_reason == "clock_drift_budget_exceeded"

    # 3. 未来时间戳超偏斜拒绝。 / Future timestamp beyond skew rejected.
    assert admission.accept(wall_env(5, source_unix_s=rx_unix + 0.06, publish_unix_s=None), 0.02, 1.0,
                            received_monotonic=rx_mono, received_unix_s=rx_unix) is None
    assert admission.last_rejection_reason == "future_timestamp_exceeds_skew"

    # 4. Cristian 中点偏斜反查：即使远端自称已同步且总年龄未超 1s，lag 0.15s > skew 0.02 + rtt/2 0.01 仍拒收。
    # Cristian midpoint cross-check catches 150 ms skew violation even when peer claims synchronized.
    assert admission.accept(
        wall_env(6, source_unix_s=rx_unix - 0.20, publish_unix_s=rx_unix - 0.15),
        0.02,
        1.0,
        received_monotonic=rx_mono,
        received_unix_s=rx_unix,
    ) is None
    assert admission.last_rejection_reason == "clock_skew_violation_detected"

    # 5. 异步推送滞后超预算拒绝。 / Delayed async push exceeding max_age_s rejected.
    assert admission.accept(
        wall_env(7, source_unix_s=rx_unix - 0.99, publish_unix_s=None, max_clock_skew_s=0.02),
        0.0,
        1.0,
        received_monotonic=rx_mono,
        received_unix_s=rx_unix,
    ) is None
    assert admission.last_rejection_reason == "wall_clock_envelope_stale"

    # 6. 合法同步墙钟接收通过。 / Valid synchronized wall_ntp envelope accepted.
    snap = admission.accept(wall_env(8), 0.02, 1.0, received_monotonic=rx_mono, received_unix_s=rx_unix)
    assert snap is not None
    assert object_available(snap, "obj_cube", now=rx_mono)


def test_receiver_local_wall_clock_step_invalidates_wall_domain():
    """Receiver detects its own wall-clock step relative to local monotonic clock and rejects wall-domain snapshots.

    接收端对比自身单调时钟与墙钟差值，检测到本地墙钟阶跃跳变时立即拒收墙钟域快照。
    """
    policy = ClockAdmissionPolicy(consumer_host_id="host-agent", max_local_wall_step_s=0.25)
    admission = SnapshotAdmission(policy=policy)
    t0_unix, t0_mono = 1_700_000_200.0, 100.0

    first = _envelope(
        sequence=1,
        clock_domain="wall_ptp",
        clock=ClockMetadata(
            clock_domain="wall_ptp",
            clock_id="ptp-1",
            host_id="host-control",
            sync_status="synchronized",
            max_clock_skew_s=0.005,
            source_unix_s=t0_unix - 0.01,
            publish_unix_s=t0_unix - 0.005,
            measurement_source="ptp_declared",
        ),
        object_evidence={"obj_cube": _remote_ev(age_wall_s=0.01, captured_unix_s=t0_unix - 0.01)},
    )
    assert admission.accept(first, 0.01, 1.0, received_monotonic=t0_mono, received_unix_s=t0_unix) is not None

    # 本地单调时钟只前进 0.1s，但本地 UTC 墙钟回跳 2.0s。 / Local monotonic +0.1s while local UTC jumps back 2.0s.
    jumped_unix = t0_unix - 2.0
    second = _envelope(
        sequence=2,
        clock_domain="wall_ptp",
        clock=ClockMetadata(
            clock_domain="wall_ptp",
            clock_id="ptp-1",
            host_id="host-control",
            sync_status="synchronized",
            max_clock_skew_s=0.005,
            source_unix_s=jumped_unix - 0.01,
            publish_unix_s=jumped_unix - 0.005,
            measurement_source="ptp_declared",
        ),
        object_evidence={"obj_cube": _remote_ev(age_wall_s=0.01, captured_unix_s=jumped_unix - 0.01)},
    )
    assert admission.accept(second, 0.01, 2.0, received_monotonic=t0_mono + 0.1, received_unix_s=jumped_unix) is None
    assert admission.last_rejection_reason == "local_wall_clock_step_detected"


def test_clock_id_domain_or_calibration_change_requires_explicit_epoch():
    """Changing clock_id, clock_domain, host_id, or object_calibration_id within the same epoch is rejected.

    同一代次内静默切换时钟 ID、时钟域、主机或标定摘要必须拒绝；仅允许显式切换新 epoch。
    """
    admission = SnapshotAdmission()
    assert admission.accept(_envelope(sequence=1), 0.01, 1.0, received_monotonic=10.0) is not None

    assert admission.accept(
        _envelope(
            sequence=2,
            clock=ClockMetadata(
                clock_domain="simulation", clock_id="clk-sim-rotated", host_id="host-control",
                measurement_source="simulation_clock",
            ),
        ),
        0.01,
        2.0,
        received_monotonic=10.1,
    ) is None
    assert admission.last_rejection_reason == "clock_id_changed_without_epoch"

    assert admission.accept(
        _envelope(
            sequence=3,
            clock_domain="rpc_rtt_bounded",
            clock=ClockMetadata(
                clock_domain="rpc_rtt_bounded", clock_id="clk-sim-1", host_id="host-control",
                measurement_source="rpc_rtt_probe",
            ),
        ),
        0.01,
        3.0,
        received_monotonic=10.2,
    ) is None
    assert admission.last_rejection_reason == "clock_domain_changed_without_epoch"

    # 显式切换新 epoch 后允许切换新时钟 ID 与新标定摘要，且旧 epoch 永久退役。
    # Advancing to a new epoch admits rotated clock_id/calibration and retires the old epoch.
    rotated = _envelope(
        sequence=4,
        epoch="scene-2",
        calibration_version="cal-sha-2",
        object_calibration_id="cal-sha-2",
        object_scene_epoch="boot:ctrl:scene2",
        clock=ClockMetadata(
            clock_domain="simulation", clock_id="clk-sim-2", host_id="host-control",
            measurement_source="simulation_clock",
        ),
        object_evidence={"obj_cube": _remote_ev(calibration_id="cal-sha-2", scene_epoch="boot:ctrl:scene2")},
    )
    assert admission.accept(rotated, 0.01, 4.0, received_monotonic=10.3) is not None
    assert admission.accept(_envelope(sequence=5, epoch="scene-1"), 0.01, 5.0, received_monotonic=10.4) is None
    assert admission.last_rejection_reason == "retired_epoch"


def test_remote_object_at_simulation_deadline_is_omitted_when_subtraction_rounds_down():
    captured = 31.789207533
    deadline = captured + 2.0
    evidence = _remote_ev(captured_sim_t=captured, age_sim_s=deadline - captured)
    envelope = _envelope(capture_timestamp=deadline, object_evidence={"obj_cube": evidence})
    snap = SnapshotAdmission().accept(envelope, 0.01)
    assert snap is not None and "obj_cube" not in snap.objects


def test_synchronized_object_at_unix_deadline_is_omitted_without_transit_delay():
    captured = 31.789207533
    deadline = captured + 2.0
    clock = ClockMetadata(clock_domain="wall_ntp", clock_id="ntp-1", host_id="host-control",
                          sync_status="synchronized", max_clock_skew_s=0.0, clock_drift_ppm=0.0,
                          source_unix_s=deadline, publish_unix_s=deadline, measurement_source="ntp_declared")
    evidence = _remote_ev(captured_unix_s=captured, age_wall_s=deadline - captured, ttl_wall_s=2.0)
    envelope = _envelope(clock_domain="wall_ntp", clock=clock, object_evidence={"obj_cube": evidence})
    snap = SnapshotAdmission().accept(envelope, 0.0, received_unix_s=deadline)
    assert snap is not None and "obj_cube" not in snap.objects


def test_per_object_rtt_skew_compounding_and_local_monotonic_rebase():
    """Per-object ages compound producer age + RTT + skew and rebase exact remaining TTL onto receiver monotonic time.

    逐对象鲜度叠加生产端年龄、RTT 与偏斜上界；过期对象入站即剔除，新鲜对象精确重基准剩余 TTL 至接收端本地单调时钟。
    """
    policy = ClockAdmissionPolicy(consumer_host_id="host-agent", max_age_s=1.0, max_allowed_skew_s=0.1)
    admission = SnapshotAdmission(policy=policy)

    env = _envelope(
        clock_domain="rpc_rtt_bounded",
        clock=ClockMetadata(
            clock_domain="rpc_rtt_bounded",
            clock_id="clk-remote",
            host_id="host-vision-edge",
            max_clock_skew_s=0.05,
            measurement_source="rpc_rtt_probe",
        ),
        objects={
            "obj_fresh": {"pos": (0.0, 0.0, 0.1), "quat": (1.0, 0.0, 0.0, 0.0)},
            "obj_expired_in_transit": {"pos": (0.0, 0.0, 0.1), "quat": (1.0, 0.0, 0.0, 0.0)},
        },
        object_evidence={
            # 0.20 + rtt 0.15 + skew 0.05 = 0.40 -> 剩余 TTL 精确为 0.60s。
            # 0.20 + rtt 0.15 + skew 0.05 = 0.40 -> exact remaining TTL is 0.60 s.
            "obj_fresh": _remote_ev(age_wall_s=0.20, ttl_wall_s=1.0),
            # 0.85 + rtt 0.15 + skew 0.05 = 1.05 >= 1.0 -> 入站立即淘汰。
            # 0.85 + rtt 0.15 + skew 0.05 = 1.05 >= 1.0 -> dropped upon arrival.
            "obj_expired_in_transit": _remote_ev(age_wall_s=0.85, ttl_wall_s=1.0),
        },
    )

    # 故意选用小于年龄的极小本地 monotonic (0.05)，验证不会因钳位到 0 而低估年龄。
    # Use small rx_mono (0.05 < conservative_age) to verify zero-clamping never underestimates elapsed age.
    snap = admission.accept(env, round_trip_s=0.15, request_started=1.0, received_monotonic=0.05)
    assert snap is not None
    assert "obj_expired_in_transit" not in snap.objects
    assert not object_available(snap, "obj_expired_in_transit", now=0.05)
    assert eval_state_predicate(
        {"predicate": "object_in_region", "args": {"object": "obj_expired_in_transit", "region": "bin_region"}},
        snap,
    ) == UNKNOWN

    assert "obj_fresh" in snap.objects
    assert snap.object_evidence is not None
    assert snap.object_evidence["obj_fresh"].captured_monotonic == pytest.approx(0.05)
    assert snap.object_evidence["obj_fresh"].ttl_wall_s == pytest.approx(0.60)
    assert object_available(snap, "obj_fresh", now=0.64) is True
    assert object_available(snap, "obj_fresh", now=0.66) is False
    # 当缓存快照滞留至剩余 TTL 耗尽后，谓词自动转 UNKNOWN。 / Cached snapshot flips predicate to UNKNOWN once rebased TTL expires.
    snap_at_expiry = admission.accept(
        _envelope(
            sequence=2,
            clock_domain="rpc_rtt_bounded",
            clock=env.clock,
            objects={"obj_fresh": {"pos": (0.0, 0.0, 0.1), "quat": (1.0, 0.0, 0.0, 0.0)}},
            object_evidence={"obj_fresh": _remote_ev(age_wall_s=0.20, ttl_wall_s=1.0)},
        ),
        round_trip_s=0.15,
        request_started=2.0,
        received_monotonic=0.05,
    )
    assert snap_at_expiry is not None
    # 直接使用真实时钟前移检验：将 captured_monotonic 设为很早的值模拟缓存超时。
    # Simulate local wall clock moving past the rebased remaining TTL.
    assert eval_state_predicate(
        {"predicate": "object_in_region", "args": {"object": "obj_fresh", "region": "bin_region"}},
        snap_at_expiry,
    ) == UNKNOWN  # 真实 time.monotonic() 远大于 0.05 + 0.60。 / Real time.monotonic() exceeds 0.05 + 0.60.

    # 使用接近真实 time.monotonic() 接收的快照：立即求值 SAT。 / Snapshot received at current monotonic evaluates SAT.
    import time as _time

    live_snap = admission.accept(
        _envelope(
            sequence=3,
            clock_domain="rpc_rtt_bounded",
            clock=env.clock,
            objects={"obj_fresh": {"pos": (0.0, 0.0, 0.1), "quat": (1.0, 0.0, 0.0, 0.0)}},
            object_evidence={"obj_fresh": _remote_ev(age_wall_s=0.20, ttl_wall_s=1.0)},
        ),
        round_trip_s=0.15,
        request_started=3.0,
        received_monotonic=_time.monotonic(),
    )
    assert live_snap is not None
    assert eval_state_predicate(
        {"predicate": "object_in_region", "args": {"object": "obj_fresh", "region": "bin_region"}},
        live_snap,
    ) == SAT
