"""Durable task history, never an automatic resume authority. 持久任务历史，不授予自动续跑权限。"""

from __future__ import annotations

import json
import time
from uuid import NAMESPACE_URL, uuid4, uuid5

from embodied.contracts.effect import effect_digest
from embodied.contracts.operation import OperationIdentity
from embodied.contracts.task import TaskJournalError, TaskPlanInput
from embodied.runtime.operations import MAX_RESULT_BYTES, request_digest


def require_journal(ledger):
    if ledger.schema_version != 2:
        raise TaskJournalError("task_journal_upgrade_required")


def event(ledger, task_id, revision, kind, payload, step_id=None):
    body = {"api_version": "embodied.dev/v1alpha1", "task_id": task_id, "revision": revision,
            "step_id": step_id, "event_type": kind, "body": payload, "recorder_boot_id": ledger.boot_id}
    body["digest"] = effect_digest(body)
    encoded = json.dumps(body, ensure_ascii=False, allow_nan=False)
    size = len(encoded.encode())
    stored = ledger._db.execute("SELECT COALESCE(SUM(length(CAST(payload_json AS BLOB))),0) FROM task_events WHERE task_id=?",
                                (task_id,)).fetchone()[0]
    if size > 65536 or kind != "task_interrupted" and stored + size > MAX_RESULT_BYTES:
        raise TaskJournalError("task_event_too_large")
    ledger._db.execute("INSERT INTO task_events VALUES(?,?,?,?,?,?,?)",
                       (uuid4().hex, task_id, revision, step_id, kind, encoded, time.time()))


def plans(ledger, task_id):
    require_journal(ledger)
    rows = ledger._db.execute("SELECT * FROM task_plans WHERE task_id=? ORDER BY revision", (task_id,)).fetchall()
    result = []
    for row in rows:
        value = json.loads(row["plan_json"])
        if (effect_digest(value) != row["digest"] or value.get("task_id") != task_id
                or value.get("revision") != row["revision"] or value.get("api_version") != "embodied.dev/v1alpha1"):
            raise TaskJournalError("task_plan_corrupt")
        if value.get("recorder_boot_id") != row["recorder_boot_id"]:
            raise TaskJournalError("task_plan_boot_corrupt")
        TaskPlanInput.model_validate({key: value[key] for key in ("api_version", "task_id", "revision", "goal")} | {
            "steps": [{key: step[key] for key in ("id", "skill", "params", "depends_on", "param_refs", "operation_id")}
                      for step in value["steps"]]})
        if any(effect_digest(step["manifest"]) != step["manifest_digest"] for step in value["steps"]):
            raise TaskJournalError("task_manifest_corrupt")
        result.append({"plan": value, "recorder_boot_id": row["recorder_boot_id"], "digest": row["digest"]})
    return result


def events(ledger, task_id):
    rows = ledger._db.execute("SELECT * FROM task_events WHERE task_id=? ORDER BY rowid", (task_id,)).fetchall()
    result = []
    for row in rows:
        value = json.loads(row["payload_json"])
        if (effect_digest({key: item for key, item in value.items() if key != "digest"}) != value["digest"]
                or value.get("api_version") != "embodied.dev/v1alpha1"
                or any(value.get(key) != row[key] for key in ("task_id", "revision", "step_id", "event_type"))
                or row["event_type"] not in {
            "plan_recorded", "step_intent", "step_reported", "task_finished", "task_interrupted"
        }):
            raise TaskJournalError("task_event_corrupt")
        result.append({"event_type": row["event_type"], "step_id": row["step_id"], "revision": row["revision"],
                       "payload": value["body"], "recorder_boot_id": value["recorder_boot_id"]})
    return result


def lifecycle(history):
    for item in reversed(history):
        if item["event_type"] == "task_finished":
            return "FINISHED"
        if item["event_type"] == "task_interrupted":
            return "INTERRUPTED"
    return "ACTIVE"


def recover(ledger):
    if ledger.schema_version != 2:
        return
    for row in ledger._db.execute("SELECT DISTINCT task_id FROM task_plans").fetchall():
        task_id = row[0]
        try:
            saved = plans(ledger, task_id)
            history = events(ledger, task_id)
        except (ValueError, KeyError, TypeError, TaskJournalError):
            continue
        if saved and lifecycle(history) == "ACTIVE" and saved[-1]["recorder_boot_id"] != ledger.boot_id:
            with ledger._db:
                event(ledger, task_id, saved[-1]["plan"]["revision"], "task_interrupted",
                      {"reason": "runtime_reopened", "previous_boot_id": saved[-1]["recorder_boot_id"]})


def _deferred(step, name, value):
    return (isinstance(value, str) and (value.startswith("${") and value.endswith("}") or value.startswith("$ref."))
            or name in step.param_refs and (name not in step.params or str(value).strip() == step.param_refs[name].strip()))


def record_plan(registry, payload):
    ledger = registry.ledger
    require_journal(ledger)
    request = TaskPlanInput.model_validate(payload)
    previous = plans(ledger, request.task_id)
    known = {s["id"] for p in previous for s in p["plan"]["steps"] if p["plan"]["revision"] < request.revision}
    identities = [s.id for s in request.steps]
    if len(set(identities)) != len(identities) or set(identities) & known:
        raise TaskJournalError("task_step_identity_reused")
    allowed = known | set(identities)
    canonical = []
    for step in request.steps:
        manifest = registry.get(step.skill)
        if (set(step.params) | set(step.param_refs)) - set(manifest.params):
            raise TaskJournalError("task_unknown_params")
        if any(dep not in allowed or dep == step.id for dep in step.depends_on):
            raise TaskJournalError("task_dependency_invalid")
        params = dict(step.params)
        for name, spec in manifest.params.items():
            if name not in params and name not in step.param_refs and spec.default is not None:
                params[name] = spec.default
            if name not in params and name not in step.param_refs and spec.required:
                raise TaskJournalError("task_missing_params")
            if name in params and not _deferred(step, name, params[name]):
                spec.validate_value(params[name])
        operation_id = step.operation_id or uuid5(NAMESPACE_URL, f"{request.task_id}:{step.id}").hex
        OperationIdentity(task_id=request.task_id, operation_id=operation_id, plan_revision=request.revision)
        canonical.append({**step.model_dump(), "params": params, "operation_id": operation_id,
                          "manifest": manifest.model_dump(mode="json"), "manifest_digest": effect_digest(manifest.model_dump(mode="json"))})
    pending = {s["id"]: set(s["depends_on"]) - known for s in canonical}
    while pending:
        ready = {key for key, deps in pending.items() if not deps}
        if not ready:
            raise TaskJournalError("task_dependency_cycle")
        pending = {key: deps - ready for key, deps in pending.items() if key not in ready}
    if len({s["operation_id"] for s in canonical}) != len(canonical):
        raise TaskJournalError("task_operation_identity_reused")
    controller = registry._execution_controller
    context = {} if controller is None else {"controller_boot_id": controller.boot_id,
        "scene_epoch": controller.sim.state_epoch, "profile_digest": controller.profile.digest}
    body = {**request.model_dump(), "steps": canonical, "robot_id": registry.robot_id, "context": context,
            "recorder_boot_id": ledger.boot_id}
    digest = effect_digest(body)
    encoded = json.dumps(body, ensure_ascii=False, allow_nan=False)
    total = sum(len(json.dumps(p["plan"], ensure_ascii=False).encode()) for p in previous
                if p["plan"]["revision"] != request.revision)
    if len(encoded.encode()) + total > MAX_RESULT_BYTES or len(previous) >= 8 and request.revision > previous[-1]["plan"]["revision"]:
        raise TaskJournalError("task_plan_too_large")
    existing = next((p for p in previous if p["plan"]["revision"] == request.revision), None)
    if existing:
        if existing["digest"] != digest:
            raise TaskJournalError("task_revision_conflict")
        return digest
    if previous and (previous[-1]["recorder_boot_id"] != ledger.boot_id or lifecycle(events(ledger, request.task_id)) != "ACTIVE"):
        raise TaskJournalError("task_requires_recovery_review")
    if previous and previous[-1]["plan"]["context"] != context:
        raise TaskJournalError("task_world_context_changed")
    if request.revision != (previous[-1]["plan"]["revision"] + 1 if previous else 1):
        raise TaskJournalError("task_revision_out_of_order")
    prior_ops = {s["operation_id"] for p in previous for s in p["plan"]["steps"]}
    if prior_ops & {s["operation_id"] for s in canonical}:
        raise TaskJournalError("task_operation_identity_reused")
    if len(prior_ops) + len(canonical) > 512:
        raise TaskJournalError("task_step_capacity_exceeded")
    now = time.time()
    with ledger._db:
        ledger._db.execute("BEGIN IMMEDIATE")
        task = ledger._db.execute("SELECT robot_id FROM tasks WHERE task_id=?", (request.task_id,)).fetchone()
        if task and task[0] != registry.robot_id:
            raise TaskJournalError("task_robot_mismatch")
        if task and not previous and ledger._db.execute("SELECT 1 FROM operations WHERE task_id=?", (request.task_id,)).fetchone():
            raise TaskJournalError("legacy_task_cannot_gain_a_plan")
        ledger._db.execute("INSERT INTO tasks VALUES(?,?,?,'OPEN',?,?) ON CONFLICT(task_id) DO UPDATE SET plan_revision=excluded.plan_revision,updated_at=excluded.updated_at",
                           (request.task_id, registry.robot_id, request.revision, now, now))
        ledger._db.execute("INSERT INTO task_plans VALUES(?,?,?,?,?,?)",
                           (request.task_id, request.revision, digest, encoded, ledger.boot_id, now))
        event(ledger, request.task_id, request.revision, "plan_recorded", {"digest": digest})
    return digest


def active_step(registry, task_id, revision, step_id):
    saved = plans(registry.ledger, task_id)
    if (not saved or saved[-1]["recorder_boot_id"] != registry.ledger.boot_id
            or saved[-1]["plan"]["revision"] != revision
            or lifecycle(events(registry.ledger, task_id)) != "ACTIVE"):
        raise TaskJournalError("task_requires_recovery_review")
    context = saved[-1]["plan"]["context"]
    controller = registry._execution_controller
    if context and (controller is None or context != {"controller_boot_id": controller.boot_id,
            "scene_epoch": controller.sim.state_epoch, "profile_digest": controller.profile.digest}):
        raise TaskJournalError("task_world_context_changed")
    step = next((s for s in saved[-1]["plan"]["steps"] if s["id"] == step_id), None)
    if step is None:
        raise TaskJournalError("task_step_not_found")
    if effect_digest(registry.get(step["skill"]).model_dump(mode="json")) != step["manifest_digest"]:
        raise TaskJournalError("task_manifest_changed")
    return step


def historical_operations(registry, task_id, saved, history):
    from types import SimpleNamespace

    from embodied.cognition.executor import DagExecutor
    from embodied.cognition.plan import Step

    specs = {s["id"]: s for p in saved for s in p["plan"]["steps"]}
    resolved = {}
    for name, spec in specs.items():
        op = registry.ledger.get(spec["operation_id"])
        if op and op.task_id == task_id and op.skill_id == spec["skill"]:
            resolved[name] = op
    receipts = {e["step_id"]: e["payload"] for e in history if e["event_type"] == "step_reported"}
    resolver = DagExecutor(registry)
    # 别名只能引用同任务中已有且请求摘要一致的实际调用，不能靠客户端 OK 创造执行。
    # Aliases must match a real same-task call and request digest; client OK cannot create execution.
    for _ in range(len(specs)):
        added = False
        done = {name: SimpleNamespace(data=op.result.get("data", {})) for name, op in resolved.items()
                if op.state == "RETURNED" and op.result and op.result.get("ok")}
        for name, spec in specs.items():
            if name in resolved or name not in receipts:
                continue
            if any(not dependency_proven(registry, specs[dep], resolved.get(dep)) for dep in spec["depends_on"]):
                continue
            alias = receipts[name].get("operation_id")
            op = next((o for o in resolved.values() if o.operation_id == alias), None)
            if op is None or op.skill_id != spec["skill"]:
                continue
            try:
                step = Step(id=name, skill=spec["skill"], params=dict(spec["params"]), param_refs=dict(spec["param_refs"]))
                resolver._resolve_param_refs(step, done)
                manifest = registry.get(spec["skill"])
                params = registry._validate_params(manifest, step.params)
                digest = request_digest(step.skill, params, manifest.model_dump(mode="json"), registry.robot_id)
                if digest == op.request_digest:
                    resolved[name] = op
                    added = True
            except (ValueError, RuntimeError, AttributeError):
                continue
        if not added:
            break
    return specs, resolved


def dependency_proven(registry, spec, op):
    from embodied.cognition.verify import SAT, eval_schema

    if op is None or op.state != "RETURNED" or not op.result or not op.result.get("ok"):
        return False
    if effect_digest(registry.get(spec["skill"]).model_dump(mode="json")) != spec["manifest_digest"]:
        return False
    verification = spec["manifest"].get("verification", {})
    if verification.get("mode") == "state_predicate":
        return bool(op.effect_evidence and op.effect_evidence["verdict"] == SAT)
    if verification.get("mode") == "schema":
        return eval_schema(verification.get("expect", {}), op.result.get("data", {})) == SAT
    return True


def record_intent(registry, task_id, revision, step_id, params):
    step = active_step(registry, task_id, revision, step_id)
    manifest = registry.get(step["skill"])
    actual = registry._validate_params(manifest, params)
    raw = TaskPlanInput.model_validate({"task_id": task_id, "revision": revision, "steps": [
        {key: step[key] for key in ("id", "skill", "params", "depends_on", "param_refs", "operation_id")}
    ]}).steps[0]
    for name, value in step["params"].items():
        if not _deferred(raw, name, value) and actual.get(name) != value:
            raise TaskJournalError("task_literal_param_changed")
    digest = request_digest(step["skill"], actual, manifest.model_dump(mode="json"), registry.robot_id)
    body = {"operation_id": step["operation_id"], "skill": step["skill"], "params": actual, "request_digest": digest}
    previous = [e for e in events(registry.ledger, task_id) if e["step_id"] == step_id and e["event_type"] == "step_intent"]
    if previous:
        if previous[-1]["payload"] != body:
            raise TaskJournalError("task_intent_conflict")
        return
    with registry.ledger._db:
        event(registry.ledger, task_id, revision, "step_intent", body, step_id)


def validate_dispatch(registry, identity, digest):
    ledger = registry.ledger
    if ledger.schema_version != 2:
        return
    saved = plans(ledger, identity.task_id)
    if not saved:
        return
    step = next((s for s in saved[-1]["plan"]["steps"] if s["operation_id"] == identity.operation_id), None)
    if step is None:
        raise TaskJournalError("operation_not_in_task_plan")
    active_step(registry, identity.task_id, identity.plan_revision, step["id"])
    if ledger._db.execute("SELECT 1 FROM operations WHERE task_id=? AND state='OUTCOME_UNKNOWN'",
                          (identity.task_id,)).fetchone():
        raise TaskJournalError("task_uncertain_operation_requires_review")
    specs, completed = historical_operations(registry, identity.task_id, saved, events(ledger, identity.task_id))
    for dependency in step["depends_on"]:
        op = completed.get(dependency)
        if not dependency_proven(registry, specs[dependency], op):
            raise TaskJournalError("task_dependency_unconfirmed")
    intents = [e for e in events(ledger, identity.task_id) if e["step_id"] == step["id"] and e["event_type"] == "step_intent"]
    if (not intents or intents[-1]["payload"]["request_digest"] != digest
            or intents[-1]["payload"]["operation_id"] != identity.operation_id):
        raise TaskJournalError("task_intent_missing_or_changed")


def record_result(registry, task_id, revision, step_id, result):
    active_step(registry, task_id, revision, step_id)
    allowed = {"status", "detail", "error", "operation_id", "attempt_id", "invocation_state", "stop_state", "effect_evidence_id", "verify_verdict"}
    if not isinstance(result, dict) or set(result) - allowed:
        raise TaskJournalError("invalid_task_receipt")
    if (result.get("status") not in {"ok", "failed", "skipped", "need_confirm"}
            or any(not isinstance(result.get(key, ""), str) or len(result.get(key, "")) > 4096
                   for key in ("detail", "error", "operation_id", "attempt_id", "invocation_state", "stop_state"))):
        raise TaskJournalError("invalid_task_receipt_values")
    prior = [e for e in events(registry.ledger, task_id) if e["step_id"] == step_id and e["event_type"] == "step_reported"]
    if prior:
        if prior[-1]["payload"] != result:
            raise TaskJournalError("task_receipt_conflict")
        return
    with registry.ledger._db:
        event(registry.ledger, task_id, revision, "step_reported", result, step_id)


def finish_task(registry, task_id, *, interrupted=False):
    ledger = registry.ledger
    if ledger.schema_version != 2:
        return
    saved = plans(ledger, task_id)
    if not saved:
        return
    if saved[-1]["recorder_boot_id"] != ledger.boot_id:
        raise TaskJournalError("task_requires_recovery_review")
    if lifecycle(events(ledger, task_id)) != "ACTIVE":
        return
    with ledger._db:
        event(ledger, task_id, saved[-1]["plan"]["revision"], "task_interrupted" if interrupted else "task_finished",
              {"reason": "caller_interrupted" if interrupted else "planner_turn_finished"})


def report_task(registry, task_id):
    ledger = registry.ledger
    task = ledger._db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if task is None:
        return {"found": False, "task_id": task_id, "automatic_resume_allowed": False}
    operation_ids = [r[0] for r in ledger._db.execute("SELECT operation_id FROM operations WHERE task_id=? ORDER BY rowid", (task_id,))]
    base = {"found": True, "api_version": "embodied.dev/v1alpha1", "task_id": task_id, "robot_id": task["robot_id"],
            "ledger_state": task["state"], "automatic_resume_allowed": False, "read_only": True,
            "workflow": "LEGACY", "plans": [], "steps": [], "untracked_operations": operation_ids,
            "issues": ["plan_unavailable"], "recommendation": "manual_review"}
    try:
        saved = plans(ledger, task_id) if ledger.schema_version == 2 else []
        if not saved:
            return base
        history = events(ledger, task_id)
    except (ValueError, KeyError, TypeError, TaskJournalError):
        return dict(base, workflow="CORRUPT", issues=["task_journal_corrupt"])
    base.update(workflow=lifecycle(history), plans=[p["plan"] for p in saved], issues=[],
                same_runtime=saved[-1]["recorder_boot_id"] == ledger.boot_id)
    context = saved[-1]["plan"]["context"]
    controller = registry._execution_controller
    base["context_matches"] = not context or controller is not None and context == {
        "controller_boot_id": controller.boot_id, "scene_epoch": controller.sim.state_epoch, "profile_digest": controller.profile.digest}
    if not base["context_matches"]:
        base["issues"].append("world_context_changed")
    tracked = set()
    for saved_plan in saved:
        for step in saved_plan["plan"]["steps"]:
            tracked.add(step["operation_id"])
            relevant = [e for e in history if e["step_id"] == step["id"]]
            intent = next((e["payload"] for e in reversed(relevant) if e["event_type"] == "step_intent"), None)
            receipt = next((e["payload"] for e in reversed(relevant) if e["event_type"] == "step_reported"), None)
            operation = ledger.get(step["operation_id"])
            try:
                compatible = effect_digest(registry.get(step["skill"]).model_dump(mode="json")) == step["manifest_digest"]
            except Exception:
                compatible = False
            item = {"id": step["id"], "revision": saved_plan["plan"]["revision"], "skill": step["skill"],
                    "operation_id": step["operation_id"], "manifest_matches": compatible, "intent": intent,
                    "reported": receipt, "call_state": operation.state if operation else "INTENT_ONLY" if intent else "NOT_DISPATCHED",
                    "attempt_id": operation.attempt_id if operation else None,
                    "stop_state": operation.stop_state if operation else "NOT_REQUESTED",
                    "historical_effect": {key: operation.effect_evidence[key] for key in (
                        "evidence_id", "verdict", "reason", "source", "observer_boot_id", "scene_epoch", "digest"
                    )} if operation and operation.effect_evidence else None,
                    "current_effect": {"verdict": "unknown", "reason": "historical_query_only", "eligible": False}}
            if operation and (operation.task_id != task_id or operation.skill_id != step["skill"]
                              or intent and operation.request_digest != intent["request_digest"]):
                item["call_state"] = "IDENTITY_MISMATCH"
            base["steps"].append(item)
            if not compatible:
                base["issues"].append("manifest_changed")
            if item["call_state"] in {"RUNNING", "OUTCOME_UNKNOWN", "INTENT_ONLY", "IDENTITY_MISMATCH"}:
                base["issues"].append(item["call_state"].lower())
            if item["call_state"] == "OUTCOME_UNKNOWN" and item["stop_state"] != "MOTION_STOPPED_OBSERVED":
                base["issues"].append("stop_unconfirmed")
    base["untracked_operations"] = [oid for oid in operation_ids if oid not in tracked]
    if base["untracked_operations"]:
        base["issues"].append("untracked_operations")
    if base["workflow"] == "INTERRUPTED" or not base["same_runtime"]:
        base["issues"].append("recovery_review_required")
    base["issues"] = sorted(set(base["issues"]))
    base["recommendation"] = "confirm_stop_before_effect_review" if "stop_unconfirmed" in base["issues"] else "manual_review"
    return base
