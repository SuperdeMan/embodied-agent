"""Reject stale, reordered, skewed, or retired cross-host snapshots (D019/D036).

拒绝过期、乱序、时钟偏斜或旧代次跨机参考快照，并将感知对象剩余 TTL 重基准至接收端单调时钟（D019/D036）。
"""

from __future__ import annotations

import math
import time
from typing import Callable
from uuid import uuid4

from embodied.cognition.world_state import Region, WorldSnapshot
from embodied.contracts.object_state import ObjectContext, ObjectEvidence
from embodied.contracts.observation import ClockAdmissionPolicy, ObservationEnvelope
from embodied.control.hal import Pose


class SnapshotAdmission:
    """Receiver-side freshness, ordering, and cross-host clock-domain gate.

    接收端快照鲜度、单调序与跨主机时钟域准入门禁。
    """

    def __init__(
        self,
        max_age_s: float | None = None,
        *,
        policy: ClockAdmissionPolicy | None = None,
        local_clock: Callable[[], float] | None = None,
        local_unix_clock: Callable[[], float] | None = None,
    ):
        if policy is None:
            limit = 1.0 if max_age_s is None else max_age_s
            if not math.isfinite(limit) or limit <= 0:
                raise ValueError("snapshot age limit must be positive and finite")
            self.policy = ClockAdmissionPolicy(
                consumer_host_id=f"consumer:{uuid4().hex[:12]}",
                max_age_s=limit,
            )
        else:
            if max_age_s is not None:
                if not math.isfinite(max_age_s) or max_age_s <= 0:
                    raise ValueError("snapshot age limit must be positive and finite")
                self.policy = policy.model_copy(update={"max_age_s": max_age_s})
            else:
                self.policy = policy
        self.max_age_s = self.policy.max_age_s
        self._local_clock = time.monotonic if local_clock is None else local_clock
        self._local_unix_clock = time.time if local_unix_clock is None else local_unix_clock
        self._receiver_clock_id = f"rx:{uuid4().hex}"
        self._current: ObservationEnvelope | None = None
        self._retired: set[tuple[str, str]] = set()
        self._last_request_start = -math.inf
        self._last_wall_rx_mono: float | None = None
        self._last_wall_rx_unix: float | None = None
        self.last_rejection_reason: str = ""

    def _reject(self, reason: str) -> None:
        self.last_rejection_reason = reason
        return None

    def accept(
        self,
        envelope: ObservationEnvelope,
        round_trip_s: float,
        request_started: float = 0.0,
        *,
        received_monotonic: float | None = None,
        received_unix_s: float | None = None,
    ) -> WorldSnapshot | None:
        """Validate envelope freshness/clock domain and rebase object TTLs onto local monotonic time.

        校验包络鲜度、代次与跨机时钟域，并将感知对象的剩余墙钟 TTL 重基准到接收端本地单调时钟。
        """
        if not math.isfinite(round_trip_s) or round_trip_s < 0:
            return self._reject("invalid_round_trip")
        if round_trip_s + envelope.capture_duration_s > self.max_age_s:
            return self._reject("rtt_plus_capture_exceeds_max_age")
        if not math.isfinite(request_started) or request_started < self._last_request_start:
            return self._reject("late_or_reordered_request")

        rx_mono = self._local_clock() if received_monotonic is None else received_monotonic
        rx_unix = self._local_unix_clock() if received_unix_s is None else received_unix_s
        if not math.isfinite(rx_mono) or rx_mono < 0 or not math.isfinite(rx_unix) or rx_unix <= 0:
            return self._reject("invalid_receiver_clock")

        clock = envelope.clock
        clock_uncertainty_s = 0.0
        clock_drift_factor = 0.0
        if clock is not None:
            if clock.max_clock_skew_s > self.policy.max_allowed_skew_s:
                return self._reject("clock_skew_budget_exceeded")
            if clock.clock_drift_ppm > self.policy.max_allowed_drift_ppm:
                return self._reject("clock_drift_budget_exceeded")
            clock_uncertainty_s = clock.max_clock_skew_s
            clock_drift_factor = clock.clock_drift_ppm * 1e-6

            if clock.clock_domain == "local_monotonic":
                # 严禁跨主机比较原始 monotonic 时钟。 / Never compare raw monotonic clocks across hosts.
                if not self.policy.allow_local_monotonic_same_host or clock.host_id != self.policy.consumer_host_id:
                    return self._reject("cross_host_monotonic_forbidden")
            elif clock.clock_domain in ("wall_ntp", "wall_ptp"):
                if not self.policy.allow_wall_clock_domains:
                    return self._reject("wall_clock_domain_disabled")
                if clock.sync_status != "synchronized":
                    return self._reject(f"clock_not_synchronized:{clock.sync_status}")
                if self._last_wall_rx_mono is not None and self._last_wall_rx_unix is not None:
                    d_mono = rx_mono - self._last_wall_rx_mono
                    d_unix = rx_unix - self._last_wall_rx_unix
                    if d_mono < 0 or abs(d_unix - d_mono) > self.policy.max_local_wall_step_s:
                        return self._reject("local_wall_clock_step_detected")
                assert clock.source_unix_s is not None
                if clock.source_unix_s - rx_unix > clock.max_clock_skew_s:
                    return self._reject("future_timestamp_exceeds_skew")
                if clock.publish_unix_s is not None:
                    # Cristian 中点往返偏斜反查：即使远端误报已同步也可检出超预算偏斜。
                    # Cristian midpoint RTT cross-check catches skew violations even if a peer claims synchronized.
                    midpoint_unix = rx_unix - round_trip_s / 2.0
                    if abs(clock.publish_unix_s - midpoint_unix) > clock.max_clock_skew_s + round_trip_s / 2.0:
                        return self._reject("clock_skew_violation_detected")
                raw_wall_age = max(0.0, (rx_unix - clock.source_unix_s) + clock.max_clock_skew_s)
                wall_envelope_age_s = raw_wall_age * (1.0 + clock_drift_factor)
                if max(round_trip_s, wall_envelope_age_s) + envelope.capture_duration_s > self.max_age_s:
                    return self._reject("wall_clock_envelope_stale")
            else:
                if round_trip_s + envelope.capture_duration_s + clock_uncertainty_s > self.max_age_s:
                    return self._reject("envelope_age_with_uncertainty_exceeds_max_age")

        key = (envelope.boot_id, envelope.epoch)
        if key in self._retired:
            return self._reject("retired_epoch")
        previous = self._current
        if previous is not None:
            if envelope.robot_id != previous.robot_id:
                return self._reject("robot_id_changed")
            old_key = (previous.boot_id, previous.epoch)
            if envelope.boot_id == previous.boot_id and envelope.sequence <= previous.sequence:
                return self._reject("non_monotonic_sequence")
            if key == old_key:
                if envelope.capture_timestamp < previous.capture_timestamp:
                    return self._reject("capture_timestamp_rollback")
                if envelope.calibration_version != previous.calibration_version:
                    return self._reject("calibration_changed_without_epoch")
                if envelope.source != previous.source:
                    return self._reject("source_changed_without_epoch")
                if envelope.object_calibration_id != previous.object_calibration_id:
                    return self._reject("object_calibration_changed_without_epoch")
                if (envelope.clock is None) != (previous.clock is None):
                    return self._reject("clock_metadata_changed_without_epoch")
                if envelope.clock is not None and previous.clock is not None:
                    if envelope.clock.clock_id != previous.clock.clock_id:
                        return self._reject("clock_id_changed_without_epoch")
                    if envelope.clock.clock_domain != previous.clock.clock_domain:
                        return self._reject("clock_domain_changed_without_epoch")
                    if envelope.clock.host_id != previous.clock.host_id:
                        return self._reject("clock_host_changed_without_epoch")
            else:
                self._retired.add(old_key)

        self._current = envelope
        self._last_request_start = request_started
        if clock is not None and clock.clock_domain in ("wall_ntp", "wall_ptp"):
            self._last_wall_rx_mono = rx_mono
            self._last_wall_rx_unix = rx_unix
        self.last_rejection_reason = ""

        regions = {name: Region(center=region.center, half=region.half) for name, region in envelope.regions.items()}
        if envelope.object_evidence is None:
            return WorldSnapshot(
                t=envelope.capture_timestamp,
                ee_pos=envelope.ee_pose.pos,
                gripper_opening=envelope.gripper_opening,
                objects={name: Pose(pos=pose.pos, quat=pose.quat) for name, pose in envelope.objects.items()},
                regions=regions,
            )

        # 将远端逐对象感知年龄叠加 RTT 与偏斜上界后，重基准至接收端本地单调时钟。
        # Combine producer-measured age, drift, RTT, and skew bounds, then rebase remaining TTL onto local monotonic time.
        receiver_context = ObjectContext(
            clock_id=self._receiver_clock_id,
            scene_epoch=f"{envelope.boot_id}:{envelope.epoch}:{envelope.object_scene_epoch}",
            calibration_id=envelope.object_calibration_id or envelope.calibration_version,
        )
        live_objects: dict[str, Pose] = {}
        rebased_evidence: dict[str, ObjectEvidence] = {}
        for name, pose in envelope.objects.items():
            ev = envelope.object_evidence[name]
            if ev.max_clock_skew_s > self.policy.max_allowed_skew_s:
                continue
            sim_age = max(ev.age_sim_s, envelope.capture_timestamp - ev.captured_sim_t)
            wall_age = (
                ev.age_wall_s * (1.0 + clock_drift_factor)
                + round_trip_s
                + clock_uncertainty_s
                + ev.max_clock_skew_s
            )
            if clock is not None and clock.clock_domain in ("wall_ntp", "wall_ptp") and ev.captured_unix_s is not None:
                combined_skew = clock.max_clock_skew_s + ev.max_clock_skew_s
                if ev.captured_unix_s - rx_unix > combined_skew:
                    continue
                unix_obj_age = max(0.0, (rx_unix - ev.captured_unix_s) + combined_skew) * (1.0 + clock_drift_factor)
                wall_age = max(wall_age, unix_obj_age)
            remaining_wall_s = ev.ttl_wall_s - wall_age
            if 0.0 <= sim_age < ev.ttl_sim_s and remaining_wall_s > 0.0:
                live_objects[name] = Pose(pos=pose.pos, quat=pose.quat)
                rebased_evidence[name] = ObjectEvidence(
                    clock_id=receiver_context.clock_id,
                    scene_epoch=receiver_context.scene_epoch,
                    calibration_id=receiver_context.calibration_id,
                    source=ev.source,
                    sensor_id=ev.sensor_id,
                    sequence=ev.sequence,
                    captured_sim_t=ev.captured_sim_t,
                    captured_monotonic=rx_mono,
                    ttl_sim_s=ev.ttl_sim_s,
                    ttl_wall_s=remaining_wall_s,
                )

        return WorldSnapshot(
            t=envelope.capture_timestamp,
            ee_pos=envelope.ee_pose.pos,
            gripper_opening=envelope.gripper_opening,
            objects=live_objects,
            regions=regions,
            object_evidence=rebased_evidence,
            object_context=receiver_context,
        )
