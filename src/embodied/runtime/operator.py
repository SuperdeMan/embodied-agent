"""Trusted proxy identity and one-use audited effect review. 可信代理身份及一次性可审计效果重查。"""

from __future__ import annotations

import hashlib
import ipaddress
import time
from dataclasses import dataclass
from uuid import uuid4

from embodied.contracts.effect import effect_digest
from embodied.contracts.operator import EffectAuthorizationRequest, OperatorConfirmation, OperatorPolicy
from embodied.runtime.recovery import review_state


class OperatorDenied(RuntimeError):
    pass


@dataclass(frozen=True)
class OperatorPrincipal:
    subject_hash: str
    policy_digest: str
    issuer: str = "tailscale_serve"


class OperatorAuthority:
    def __init__(self, registry, policy: OperatorPolicy, *, clock=time.monotonic):
        self.registry = registry
        self.policy = policy
        body = policy.model_dump(mode="json")
        body["subjects"] = sorted(body["subjects"])
        self.policy_digest = effect_digest(body)
        self._clock = clock
        self._pending = {}

    def authenticate(self, peer: str, headers, *, mutation: bool = False) -> OperatorPrincipal:
        try:
            address = ipaddress.ip_address(peer)
        except (ValueError, TypeError):
            raise OperatorDenied("untrusted_proxy") from None
        address = getattr(address, "ipv4_mapped", None) or address
        if address != self.policy.trusted_proxy:
            raise OperatorDenied("untrusted_proxy")
        # 只读精确头名；不合并下划线变体，不依赖可伪造的转发地址。
        # Read exact header names; do not merge underscore variants or trust forwarded addresses.
        if any("_" in key and key.lower().replace("_", "-").startswith("tailscale-") for key in headers):
            raise OperatorDenied("ambiguous_identity_headers")
        values = headers.getall("Tailscale-User-Login", [])
        if len(values) != 1 or values[0] not in self.policy.subjects:
            raise OperatorDenied("operator_not_allowed")
        if headers.getall("Host", []) != [self.policy.origin.removeprefix("https://")]:
            raise OperatorDenied("audience_mismatch")
        origins = headers.getall("Origin", [])
        if (mutation or origins) and origins != [self.policy.origin]:
            raise OperatorDenied("origin_mismatch")
        return OperatorPrincipal(hashlib.sha256(values[0].encode()).hexdigest(), self.policy_digest)

    def _principal(self, principal):
        subjects = {hashlib.sha256(s.encode()).hexdigest() for s in self.policy.subjects}
        if (not isinstance(principal, OperatorPrincipal) or principal.issuer != "tailscale_serve"
                or principal.policy_digest != self.policy_digest or principal.subject_hash not in subjects):
            raise OperatorDenied("invalid_operator_principal")

    def _target(self, request, report):
        registry = self.registry
        review = report.get("recovery_review", {})
        if (not review.get("coherent") or report.get("workflow") in {"LEGACY", "CORRUPT"}
                or not report.get("context_matches") or "guard_unavailable" in review.get("blockers", [])):
            raise OperatorDenied("review_context_unavailable")
        step = next((s for s in report.get("steps", []) if s["operation_id"] == request.operation_id), None)
        record = registry.ledger.get(request.operation_id)
        if (step is None or record is None or record.task_id != request.task_id
                or record.attempt_id != request.attempt_id or step["attempt_id"] != request.attempt_id
                or not step["manifest_matches"] or step["call_state"] == "IDENTITY_MISMATCH"
                or not step.get("intent") or step["intent"]["request_digest"] != record.request_digest):
            raise OperatorDenied("review_target_mismatch")
        controller = registry._execution_controller
        if controller is None or controller.sim.guard.halted or controller.sim.guard.limits is not controller._limits:
            raise OperatorDenied("guard_unavailable")
        # 使用现有维护准入核对停止、上下文及资源；此处不观察、不写入。
        # Reuse maintenance admission for stop/context/resources; do not observe or write here.
        _, _, lease = registry._reserve_effect(record.operation_id, record.attempt_id)
        registry.arbiter.release(lease)
        return record

    def _audit(self, record, kind, payload):
        ledger = self.registry.ledger
        with ledger._db:
            ledger._db.execute("BEGIN IMMEDIATE")
            current = ledger.get(record.operation_id)
            if (current is None or current.attempt_id != record.attempt_id
                    or current.request_digest != record.request_digest):
                raise OperatorDenied("audit_target_changed")
            size = ledger._db.execute("SELECT COALESCE(SUM(length(payload_json)),0) FROM operation_events "
                                      "WHERE operation_id=? AND event_type LIKE 'operator_%'", (record.operation_id,)).fetchone()[0]
            if size > 262144:
                raise OperatorDenied("operator_audit_capacity")
            ledger._event(record.operation_id, record.attempt_id, kind, payload)

    def _audit_bound(self, record, kind, payload, report):
        before = review_state(self.registry, report, include_storage=False)[0]
        db = self.registry.ledger._db
        changes = db.total_changes
        version = db.execute("PRAGMA data_version").fetchone()[0]
        self._audit(record, kind, payload)
        # 只容许本次审计自身的一行写入；其他身份/日志变化使授权失效。
        # Permit only this audit's single-row write; other identity or ledger changes invalidate authorization.
        if (review_state(self.registry, report, include_storage=False)[0] != before
                or db.total_changes != changes + 1 or db.execute("PRAGMA data_version").fetchone()[0] != version):
            raise OperatorDenied("review_snapshot_changed")

    async def issue(self, principal: OperatorPrincipal, request: EffectAuthorizationRequest):
        self.registry.check_thread()
        self._principal(principal)
        now = self._clock()
        self._pending = {key: value for key, value in self._pending.items() if value["deadline"] > now}
        if len(self._pending) >= 128 or sum(p["principal"] == principal for p in self._pending.values()) >= 16:
            raise OperatorDenied("operator_challenge_capacity")
        report = await self.registry.reconcile_task(request.task_id)
        if report.get("recovery_review", {}).get("state_digest") != request.state_digest:
            raise OperatorDenied("review_snapshot_changed")
        record = self._target(request, report)
        if len(self._pending) >= 128 or sum(p["principal"] == principal for p in self._pending.values()) >= 16:
            raise OperatorDenied("operator_challenge_capacity")
        challenge = uuid4().hex
        now = self._clock()
        audit = {"api_version": "embodied.dev/v1alpha1", "challenge_id": challenge, "scope": request.scope,
                 "subject_hash": principal.subject_hash, "issuer": principal.issuer, "policy_digest": self.policy_digest,
                 "task_id": record.task_id, "request_digest": record.request_digest,
                 "ledger_boot_id": self.registry.ledger.boot_id, "review_digest": request.state_digest,
                 "ttl_s": 30, "issued_unix": time.time()}
        self._audit_bound(record, "operator_authorization_issued", audit, report)
        remaining = now + 30 - self._clock()
        if remaining <= 0:
            raise OperatorDenied("operator_challenge_expired")
        self._pending[challenge] = {"principal": principal, "request": request, "record": record,
            "deadline": now + 30, "audit": audit, "report": report,
            "state": review_state(self.registry, report)[0]}
        return {"challenge_id": challenge, "scope": request.scope, "expires_in_s": remaining,
                "task_execution_authorized": False, "confirmation_required": True}

    async def consume(self, principal: OperatorPrincipal, confirmation: OperatorConfirmation):
        self.registry.check_thread()
        self._principal(principal)
        pending = self._pending.get(confirmation.challenge_id)
        if pending is None or pending["principal"] != principal:
            raise OperatorDenied("operator_challenge_unavailable")
        # 确认一旦提交即烧毁凭据，存储失败也不能重用。 / Burn on confirmation submission, even if persistence fails.
        self._pending.pop(confirmation.challenge_id)
        if self._clock() >= pending["deadline"]:
            raise OperatorDenied("operator_challenge_expired")
        if review_state(self.registry, pending["report"])[0] != pending["state"]:
            raise OperatorDenied("review_snapshot_changed")
        record = self._target(pending["request"], pending["report"])
        audit = dict(pending["audit"], confirmed=confirmation.confirmed)
        self._audit_bound(record, "operator_authorization_consumed", audit, pending["report"])
        if self._clock() >= pending["deadline"]:
            raise OperatorDenied("operator_challenge_expired")
        if not confirmation.confirmed:
            self._audit(record, "operator_authorization_finished", dict(audit, status="declined"))
            return {"status": "declined", "task_execution_authorized": False}
        try:
            updated = await self.registry.reconcile_effect_async(record.operation_id, record.attempt_id)
        except BaseException as exc:
            try:
                self._audit(record, "operator_authorization_finished", dict(audit, status="failed", error=type(exc).__name__))
            except Exception:
                pass
            raise
        self._audit(record, "operator_authorization_finished", dict(audit, status="completed",
                    effect_evidence_id=updated.effect_evidence["evidence_id"]))
        return {"status": "completed", "challenge_id": confirmation.challenge_id, "scope": "reconcile_effect",
                "operation_id": record.operation_id, "attempt_id": record.attempt_id,
                "effect_evidence_id": updated.effect_evidence["evidence_id"], "effect_verdict": updated.effect_evidence["verdict"],
                "task_execution_authorized": False}
