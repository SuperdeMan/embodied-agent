"""Trusted local effect sampling; never executes a motion. 受信本地效果采样，永不执行动作。"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from uuid import uuid4

from embodied.cognition.verify import UNKNOWN, eval_state_predicate
from embodied.cognition.world_state import WorldSnapshot
from embodied.contracts.effect import EFFECT_MAX_AGE_S, EffectEvidence, EffectRejected, effect_digest
from embodied.contracts.object_state import ObjectEvidence


class EffectObserver:
    def __init__(self, embodiment):
        self.embodiment = embodiment
        self.controller = embodiment.action_controller

    def context(self, verification: dict, params: dict) -> dict:
        return {"controller_boot_id": self.controller.boot_id, "scene_epoch": self.controller.sim.state_epoch,
                "profile_digest": self.controller.profile.digest, "verification": verification, "params": params,
                "resources": list(self.controller.profile.resources)}

    def evidence_current(self, evidence: dict) -> bool:
        if evidence["source"] != "perception":
            return True
        try:
            body = self.controller.sim.read_control_feedback()
            context = self.embodiment._identity(body)
            items = evidence["snapshot"]["object_evidence"]
            return all(ObjectEvidence.model_validate_json(json.dumps(items[name])).current(
                context, body.t, time.monotonic(),
            ) for name in evidence["snapshot"]["objects"])
        except Exception:
            return False

    def matches(self, context: dict) -> bool:
        return (context.get("controller_boot_id") == self.controller.boot_id
                and context.get("scene_epoch") == self.controller.sim.state_epoch
                and context.get("profile_digest") == self.controller.profile.digest
                and self.controller.sim.guard.limits is self.controller._limits)

    def capture(self, record, observer_boot_id: str, *, unavailable: str = "") -> EffectEvidence:
        context = record.stop_context.get("effect_context")
        if not isinstance(context, dict):
            raise EffectRejected("effect_context_unavailable")
        started = time.monotonic()
        snapshot, verdict, source, reason = {}, UNKNOWN, "unavailable", unavailable
        if not self.matches(context):
            reason = "effect_context_changed"
        if not reason:
            try:
                obs = self.embodiment.read()
                snap = WorldSnapshot.from_observation(obs)
                snapshot = {"t": snap.t, "ee_pos": snap.ee_pos, "gripper_opening": snap.gripper_opening,
                            "objects": {name: asdict(pose) for name, pose in snap.objects.items()},
                            "regions": {name: asdict(region) for name, region in snap.regions.items()},
                            "object_evidence": None if obs.object_evidence is None else
                            {name: item.model_dump(mode="json") for name, item in obs.object_evidence.items()}}
                effect_digest(snapshot)
                source = "sim_truth" if obs.object_evidence is None else "perception"
                verdict = eval_state_predicate(context["verification"].get("expect", {}), snap, context["params"])
                if not self.matches(context) or time.monotonic() - started >= EFFECT_MAX_AGE_S:
                    verdict, reason = UNKNOWN, "effect_capture_stale_or_changed"
            except Exception:
                snapshot, verdict, source, reason = {}, UNKNOWN, "unavailable", "effect_observation_unavailable"
        return EffectEvidence.seal(
            evidence_id=uuid4().hex, operation_id=record.operation_id, attempt_id=record.attempt_id,
            robot_id=record.robot_id, request_digest=record.request_digest,
            verification_digest=effect_digest(context["verification"]), observer_boot_id=observer_boot_id,
            controller_boot_id=context["controller_boot_id"], scene_epoch=context["scene_epoch"],
            profile_digest=context["profile_digest"], captured_monotonic=started, captured_unix=time.time(),
            verdict=verdict, reason=reason, source=source, snapshot=snapshot,
        )
