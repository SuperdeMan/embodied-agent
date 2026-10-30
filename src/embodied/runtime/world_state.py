"""Reject stale, reordered or retired reference snapshots. 拒绝过期、乱序和旧代次参考快照。"""

from __future__ import annotations

import math

from embodied.cognition.world_state import Region, WorldSnapshot
from embodied.contracts.observation import ObservationEnvelope
from embodied.control.hal import Pose


class SnapshotAdmission:
    def __init__(self, max_age_s: float = 1.0):
        if not math.isfinite(max_age_s) or max_age_s <= 0:
            raise ValueError("snapshot age limit must be positive and finite")
        self.max_age_s = max_age_s
        self._current: ObservationEnvelope | None = None
        self._retired: set[tuple[str, str]] = set()
        self._last_request_start = -math.inf

    def accept(self, envelope: ObservationEnvelope, round_trip_s: float,
               request_started: float = 0.0) -> WorldSnapshot | None:
        if not math.isfinite(round_trip_s) or round_trip_s < 0:
            return None
        if round_trip_s + envelope.capture_duration_s > self.max_age_s:
            return None
        if not math.isfinite(request_started) or request_started < self._last_request_start:
            return None
        key = (envelope.boot_id, envelope.epoch)
        if key in self._retired:
            return None
        previous = self._current
        if previous is not None:
            if envelope.robot_id != previous.robot_id:
                return None
            old_key = (previous.boot_id, previous.epoch)
            if envelope.boot_id == previous.boot_id and envelope.sequence <= previous.sequence:
                return None
            if key == old_key:
                if (envelope.sequence <= previous.sequence
                        or envelope.capture_timestamp < previous.capture_timestamp
                        or envelope.calibration_version != previous.calibration_version):
                    return None
            else:
                self._retired.add(old_key)
        self._current = envelope
        self._last_request_start = request_started
        return WorldSnapshot(
            t=envelope.capture_timestamp, ee_pos=envelope.ee_pose.pos,
            gripper_opening=envelope.gripper_opening,
            objects={name: Pose(pos=pose.pos, quat=pose.quat) for name, pose in envelope.objects.items()},
            regions={name: Region(center=region.center, half=region.half) for name, region in envelope.regions.items()},
        )
