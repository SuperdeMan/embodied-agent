"""Single-step recovery for authenticated operators. 已认证操作者的单步受控恢复。"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import time
from dataclasses import dataclass, replace
from typing import Callable
from uuid import uuid4

from embodied.contracts.effect import EFFECT_MAX_AGE_S, EffectEvidence
from embodied.contracts.operation import OperationIdentity
from embodied.contracts.operator import OperatorAuthorizationRequest
from embodied.runtime.operations import OperationConflict, request_digest
from embodied.runtime.operator import OperatorDenied, OperatorForbidden, OperatorPrincipal
from embodied.runtime.recovery import review_state
from embodied.runtime.rpc_auth import RpcPrincipal
from embodied.runtime.task_journal import dependency_proven

logger = logging.getLogger("runtime.step_retry")


@dataclass(frozen=True)
class _Pending:
    principal: OperatorPrincipal | RpcPrincipal
    request: OperatorAuthorizationRequest
    report: dict
    revision: int
    deadline: float
    state: str
    evidence_id: str
    guard_generation: int
    changes: int
    data_version: int


@dataclass(frozen=True)
class ConfirmedRetry:
    grant_digest: str
    pending: _Pending
    proof: EffectEvidence
    observation_deadline: float
    supervision_ok: Callable[[], bool]

    def require_current_controller(self, registry):
        controller = registry._execution_controller
        now = time.monotonic()
        if (controller is None or controller.sim.guard.halted
                or controller.sim.guard.generation != self.pending.guard_generation
                or controller.sim.guard.limits is not controller._limits
                or controller.boot_id != self.proof.controller_boot_id
                or controller.sim.state_epoch != self.proof.scene_epoch
                or controller.profile.digest != self.proof.profile_digest
                or now >= min(self.pending.deadline, self.observation_deadline)
                or not self.supervision_ok()):
            raise OperatorDenied("retry_context_changed_or_expired")


def _check_principal(principal):
    if (not isinstance(principal, (OperatorPrincipal, RpcPrincipal))
            or isinstance(principal, RpcPrincipal) and principal.role != "operator"
            or "retry_step" not in principal.scopes):
        raise OperatorForbidden("operator_scope_not_allowed")


def validate_grant(ledger, grant: ConfirmedRetry, prior, identity, digest):
    """Check durable bindings inside the same transaction that creates the attempt. 在新尝试事务内复核授权绑定。"""
    if not isinstance(grant, ConfirmedRetry):
        raise OperationConflict("invalid_retry_grant")
    pending, proof = grant.pending, grant.proof
    principal = pending.principal
    row = ledger._db.execute("SELECT * FROM recovery_grants WHERE grant_digest=?", (grant.grant_digest,)).fetchone()
    bound = {"state": "ISSUED", "task_id": identity.task_id, "operation_id": identity.operation_id,
             "plan_revision": identity.plan_revision, "previous_attempt_id": prior.attempt_id, "request_digest": digest,
             "ledger_boot_id": ledger.boot_id, "subject_hash": principal.subject_hash, "issuer": principal.issuer,
             "policy_digest": principal.policy_digest, "evidence_id": pending.evidence_id,
             "controller_boot_id": proof.controller_boot_id, "scene_epoch": proof.scene_epoch,
             "profile_digest": proof.profile_digest, "review_digest": pending.request.state_digest}
    now = time.monotonic()
    if (row is None or any(row[k] != v for k, v in bound.items())
            or ledger._db.total_changes != pending.changes
            or ledger._db.execute("PRAGMA data_version").fetchone()[0] != pending.data_version
            or prior.state != "OUTCOME_UNKNOWN" or prior.stop_state != "MOTION_STOPPED_OBSERVED"
            or not prior.effect_evidence or prior.effect_evidence["evidence_id"] != pending.evidence_id
            or proof.observer_boot_id != ledger.boot_id or proof.verdict != "unsat"
            or not ledger.valid_effect(replace(prior, effect_evidence=proof.model_dump(mode="json")))
            or not proof.captured_monotonic <= now < min(grant.observation_deadline, pending.deadline, row["deadline_monotonic"])):
        raise OperationConflict("retry_grant_invalid_or_expired")


def consume_grant(ledger, grant: ConfirmedRetry, attempt_id: str, now: float):
    updated = ledger._db.execute("""UPDATE recovery_grants SET state='CONSUMED',closed_at=?,new_attempt_id=?
        WHERE grant_digest=? AND state='ISSUED'""", (now, attempt_id, grant.grant_digest))
    if updated.rowcount != 1:
        raise OperationConflict("retry_grant_already_consumed")
    proof = grant.proof
    ledger._event(proof.operation_id, proof.attempt_id, "retry_effect_verified",
                  {"new_attempt_id": attempt_id, "grant_digest": grant.grant_digest, "effect": proof.model_dump(mode="json")})


class StepRetryAuthority:
    """Mint scoped challenges and consume the admitted intent once. 签发限定作用域的凭据并一次性消费准入意图。"""

    def __init__(self, registry):
        self.registry = registry
        self._pending: dict[str, _Pending] = {}

    def _target(self, request, report):
        registry = self.registry
        review = report.get("recovery_review", {})
        if (not review.get("coherent") or report.get("context_matches") is not True
                or report.get("workflow") not in {"FINISHED", "INTERRUPTED"} or report.get("untracked_operations")):
            raise OperatorDenied("retry_task_context_unavailable")
        steps = {s["id"]: s for s in report["steps"]}
        step = next((s for s in steps.values() if s["operation_id"] == request.operation_id), None)
        record = registry.ledger.get(request.operation_id)
        if (step is None or record is None or record.task_id != request.task_id
                or step["revision"] != review["plan_revision"] or not step["manifest_matches"]
                or step["attempt_id"] != request.attempt_id or record.attempt_id != request.attempt_id
                or step["call_state"] != "OUTCOME_UNKNOWN" or record.stop_state != "MOTION_STOPPED_OBSERVED"
                or not record.stop_evidence or record.stop_context.get("tracked_workers") is not True
                or not record.effect_evidence or record.effect_evidence["verdict"] != "unsat"
                or record.skill_id not in registry._tracked_skills):
            raise OperatorDenied("retry_target_not_eligible")
        if any(s is not step and s["call_state"] in {"RUNNING", "OUTCOME_UNKNOWN", "IDENTITY_MISMATCH"}
               for s in steps.values()):
            raise OperatorDenied("retry_other_operation_unsettled")
        manifest = registry.get(record.skill_id)
        intent = step["intent"] or {}
        if (manifest.require_confirm or manifest.verification.get("mode") != "state_predicate"
                or intent.get("request_digest") != record.request_digest
                or request_digest(record.skill_id, intent.get("params"), manifest.model_dump(mode="json"),
                                  registry.robot_id) != record.request_digest):
            raise OperatorDenied("retry_manifest_or_intent_changed")
        declared = next(s for s in report["plans"][-1]["steps"] if s["id"] == step["id"])
        specs = {s["id"]: s for plan in report["plans"] for s in plan["steps"]}
        dependencies = []
        for name in declared["depends_on"]:
            dep = steps[name]
            op = registry.ledger.get(dep["operation_id"])
            if not dep["manifest_matches"] or not dependency_proven(registry, specs[name], op):
                raise OperatorDenied("retry_dependency_unconfirmed")
            if specs[name]["manifest"].get("verification", {}).get("mode") == "state_predicate":
                dependencies.append(dep)
        controller, stop = registry._execution_controller, registry._stop_controller
        if (controller is None or stop is None or controller.sim.guard.halted
                or controller.sim.guard.limits is not controller._limits
                or record.stop_evidence["profile_digest"] != stop.profile.digest
                or getattr(controller.sim, "control_process_state", "READY") != "READY"
                or registry.stop_pending or registry.observation_pending):
            raise OperatorDenied("retry_control_unavailable")
        return record, step, manifest, dependencies

    async def issue(self, principal, request):
        _check_principal(principal)
        registry = self.registry
        registry.check_thread()
        if registry.ledger.schema_version != 3 or request.scope != "retry_step":
            raise OperatorDenied("retry_v3_required")
        report = await registry.reconcile_task(request.task_id)
        if report.get("recovery_review", {}).get("state_digest") != request.state_digest:
            raise OperatorDenied("review_snapshot_changed")
        reviewed_state = review_state(registry, report)[0]
        record, step, manifest, dependencies = self._target(request, report)
        runtime_digest, ctx = review_state(registry, report, include_storage=False)
        current = step["current_effect"]
        if (current.get("eligible") is not True or current.get("verdict") != "unsat"
                or any(s["current_effect"].get("eligible") is not True or s["current_effect"].get("verdict") != "sat"
                       for s in dependencies)):
            raise OperatorDenied("retry_fresh_effects_required")
        _, _, lease = registry._reserve_effect(record.operation_id, record.attempt_id)
        registry.arbiter.release(lease)
        now = time.monotonic()
        self._pending = {k: v for k, v in self._pending.items() if v.deadline > now}
        if len(self._pending) >= 128 or sum(p.principal == principal for p in self._pending.values()) >= 16:
            raise OperatorDenied("operator_challenge_capacity")
        token = uuid4().hex
        key = hashlib.sha256(token.encode()).hexdigest()
        deadline = now + 30.0
        review = report["recovery_review"]
        ledger = registry.ledger
        with ledger._db:
            ledger._db.execute("BEGIN IMMEDIATE")
            if review_state(registry, report)[0] != reviewed_state:
                raise OperatorDenied("review_snapshot_changed")
            ledger._db.execute("""INSERT INTO recovery_grants(
                grant_digest,subject_hash,issuer,policy_digest,task_id,plan_revision,operation_id,previous_attempt_id,
                request_digest,review_digest,ledger_boot_id,controller_boot_id,scene_epoch,profile_digest,evidence_id,
                issued_at,deadline_monotonic,state) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'ISSUED')""",
                (key, principal.subject_hash, principal.issuer, principal.policy_digest, request.task_id, step["revision"],
                 record.operation_id, record.attempt_id, record.request_digest, request.state_digest, ledger.boot_id,
                 review["controller_boot_id"], review["scene_epoch"], review["profile_digest"],
                 record.effect_evidence["evidence_id"], time.time(), deadline))
            if review_state(registry, report, include_storage=False)[0] != runtime_digest:
                raise OperatorDenied("review_snapshot_changed")
        self._pending[key] = _Pending(principal, request, report, step["revision"], deadline,
                                      review_state(registry, report)[0], record.effect_evidence["evidence_id"],
                                      ctx["guard_generation"], ledger._db.total_changes,
                                      ledger._db.execute("PRAGMA data_version").fetchone()[0])
        return {"challenge_id": token, "scope": "retry_step", "skill": manifest.name, "params": step["intent"]["params"],
                "operation_id": record.operation_id, "attempt_id": record.attempt_id, "expires_in_s": deadline - now,
                "confirmation_required": True, "task_execution_authorized": False}

    def recognizes(self, token: str) -> bool:
        return hashlib.sha256(token.encode()).hexdigest() in self._pending

    def _close_grant(self, key, state):
        db = self.registry.ledger._db
        with db:
            db.execute("UPDATE recovery_grants SET state=?,closed_at=? WHERE grant_digest=? AND state='ISSUED'",
                       (state, time.time(), key))

    async def consume(self, principal, confirmation, *, supervision_ok=lambda: True):
        _check_principal(principal)
        registry = self.registry
        registry.check_thread()
        key = hashlib.sha256(confirmation.challenge_id.encode()).hexdigest()
        pending = self._pending.get(key)
        if pending is None or pending.principal != principal:
            raise OperatorDenied("operator_challenge_unavailable")
        del self._pending[key]
        if confirmation.confirmed is not True:
            self._close_grant(key, "DECLINED")
            return {"status": "declined", "task_execution_authorized": False}
        try:
            if time.monotonic() >= pending.deadline or review_state(registry, pending.report)[0] != pending.state:
                raise OperatorDenied("retry_review_changed_or_expired")
            record, step, manifest, dependencies = self._target(pending.request, pending.report)
            proof_deadline = pending.deadline
            for dep in dependencies:
                item = registry.ledger.get(dep["operation_id"])
                evidence = item.effect_evidence if item else None
                if evidence is None:
                    raise OperatorDenied("retry_dependency_unconfirmed")
                check = await registry.check_effect_async(item.operation_id, item.attempt_id, evidence["evidence_id"],
                                                         evidence["verification_digest"])
                if not check["eligible"] or check["verdict"] != "sat":
                    raise OperatorDenied("retry_dependency_unconfirmed")
                proof_deadline = min(proof_deadline, evidence["captured_monotonic"] + EFFECT_MAX_AGE_S)
            _, observer, lease = registry._reserve_effect(record.operation_id, record.attempt_id)
            try:
                proof = await registry.observe(observer.capture, record, registry.ledger.boot_id)
                if proof.verdict != "unsat" or not await registry.observe(observer.evidence_current, proof.model_dump(mode="json")):
                    raise OperatorDenied("retry_effect_not_unsatisfied")
            finally:
                registry.arbiter.release(lease)
            if review_state(registry, pending.report)[0] != pending.state:
                raise OperatorDenied("review_snapshot_changed")
            grant = ConfirmedRetry(key, pending, proof, min(proof_deadline, proof.captured_monotonic + EFFECT_MAX_AGE_S),
                                   supervision_ok)
            grant.require_current_controller(registry)
            identity = OperationIdentity(task_id=record.task_id, operation_id=record.operation_id, plan_revision=pending.revision)
            handler = registry._skills[manifest.name][1]
            result = await registry._invoke_registered(manifest, handler, step["intent"]["params"], False, identity,
                                                       retry_grant=grant)
            if not result.attempt_id or result.attempt_id == record.attempt_id:
                raise OperatorDenied("retry_did_not_acquire_resources")
            return {"status": "attempted", "scope": "retry_step", "operation_id": record.operation_id,
                    "previous_attempt_id": record.attempt_id, "attempt_id": result.attempt_id,
                    "invocation_state": result.invocation_state,
                    "effect_verdict": result.data.get("_effect", {}).get("verdict", "unknown"),
                    "task_execution_authorized": False, "automatic_resume_allowed": False}
        except BaseException:
            try:
                self._close_grant(key, "REFUSED")
            except sqlite3.DatabaseError:
                logger.error("step retry refusal could not be persisted")
            raise
