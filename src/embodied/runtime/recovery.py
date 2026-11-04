"""Conservative review across asynchronous evidence checks. 跨异步证据检查的保守审查。"""

from __future__ import annotations

import math
import time

from embodied.contracts.effect import EFFECT_MAX_AGE_S, effect_digest
from embodied.contracts.recovery import RecoveryOperationBinding, RecoveryReview


def review_state(registry, report):
    """Fingerprint receiver-visible state without sampling or writing.

    对接收端可见状态计算指纹，不采样物理状态、不写入。
    """
    controller = registry._execution_controller
    context = {} if controller is None else {
        "controller_boot_id": controller.boot_id, "scene_epoch": controller.sim.state_epoch,
        "profile_digest": controller.profile.digest, "control_epoch": controller.epoch,
        "guard_halted": controller.sim.guard.halted,
        "guard_generation": controller.sim.guard.generation,
        "guard_matches": controller.sim.guard.limits is controller._limits,
    }
    manifests = {}
    for step in report.get("steps", []):
        entry = registry._skills.get(step["skill"])
        manifests[step["skill"]] = None if entry is None else (entry[0].model_dump(mode="json"), id(entry[1]))
    permits = {}
    for key, permit in registry._stop_contexts.items():
        with permit.access() as allowed:
            permits[key] = (allowed, permit.drained)
    state = {"ledger_boot_id": registry.ledger.boot_id, "changes": registry.ledger._db.total_changes,
             "data_version": registry.ledger._db.execute("PRAGMA data_version").fetchone()[0],
             "context": context, "manifests": manifests, "active": sorted(registry._active), "permits": permits}
    return effect_digest(state), context


def expire_effect(current, elapsed, reason):
    """Age evidence conservatively; malformed ages cannot remain eligible.

    保守累计证据年龄；异常年龄不得保留资格。
    """
    if current.get("eligible") is not True:
        if current.get("eligible") is not False:
            current.update(eligible=False, verdict="unknown", reason=reason)
            current.pop("age_s", None)
        return
    age = current.get("age_s")
    if (current.get("verdict") not in {"sat", "unsat", "unknown"}
            or type(age) not in (int, float) or not math.isfinite(age) or age < 0
            or not math.isfinite(elapsed) or elapsed < 0):
        current.update(eligible=False, verdict="unknown", reason=reason)
        current.pop("age_s", None)
        return
    current["age_s"] = age + elapsed
    if not math.isfinite(current["age_s"]) or current["age_s"] >= EFFECT_MAX_AGE_S:
        current.update(eligible=False, verdict="unknown", reason=reason)
        if not math.isfinite(current["age_s"]):
            current.pop("age_s", None)


async def assess_task(registry, task_id):
    registry.check_thread()
    before = review_state(registry, {})[0]
    boot_id = registry.ledger.boot_id
    report = registry.get_task(task_id)
    report.update(assessment="current_eligibility_only", read_only=True, automatic_resume_allowed=False)
    history_digest = effect_digest(report)
    coherent = before == review_state(registry, {})[0]
    initial, context = review_state(registry, report)
    checked = []
    for step in report.get("steps", []):
        if not coherent:
            break
        evidence = step["historical_effect"]
        if not evidence or not step["manifest_matches"] or step["call_state"] == "IDENTITY_MISMATCH":
            continue
        # 用检查起点累计等待，宁可多算，不能遗漏等待期间的老化。
        # Account from the check start: overcounting is safer than omitting age accrued while waiting.
        started = time.monotonic()
        try:
            check = await registry.check_effect_async(step["operation_id"], step["attempt_id"], evidence["evidence_id"],
                                                      effect_digest(registry.get(step["skill"]).verification))
            step["current_effect"] = {key: check[key] for key in ("eligible", "verdict", "age_s", "reason")}
        except Exception:
            step["current_effect"] = {"eligible": False, "verdict": "unknown", "reason": "recovery_check_unavailable"}
        checked.append((step["current_effect"], started))
        if review_state(registry, report)[0] != initial:
            coherent = False
            break
    coherent = coherent and review_state(registry, report)[0] == initial
    now = time.monotonic()
    for current, started in checked:
        expire_effect(current, now - started, "effect_expired_during_review")
    if not coherent:
        for step in report.get("steps", []):
            step["current_effect"] = {"eligible": False, "verdict": "unknown", "reason": "recovery_snapshot_changed"}
    blockers = {"authenticated_operator_required", "execution_recovery_not_implemented", *report.get("issues", [])}
    if not report.get("found"):
        blockers.add("task_not_found")
    if not coherent:
        blockers.add("recovery_snapshot_changed")
    if not context:
        blockers.add("controller_context_unavailable")
    if context.get("guard_halted") or context.get("guard_matches") is False:
        blockers.add("guard_unavailable")
    bindings = []
    for step in report.get("steps", []):
        # 请求摘要取历史意图；不混入异步等待后新读到的操作版本。
        # Bind the historical intent digest rather than mixing in operation versions read after awaiting.
        intent = step.get("intent") or {}
        evidence = step.get("historical_effect") or {}
        bindings.append(RecoveryOperationBinding(
            step_id=step["id"], operation_id=step["operation_id"], attempt_id=step["attempt_id"],
            request_digest=intent.get("request_digest"), call_state=step["call_state"], stop_state=step["stop_state"],
            evidence_id=evidence.get("evidence_id")))
    review = RecoveryReview(task_id=task_id, robot_id=report.get("robot_id", registry.robot_id),
        ledger_boot_id=boot_id, plan_revision=max((p["revision"] for p in report.get("plans", [])), default=0),
        controller_boot_id=context.get("controller_boot_id"), scene_epoch=context.get("scene_epoch"),
        profile_digest=context.get("profile_digest"), state_digest=effect_digest({"state": initial, "history": history_digest}),
        coherent=coherent, blockers=sorted(blockers), operations=bindings)
    report["recovery_review"] = review.model_dump(mode="json")
    return report
