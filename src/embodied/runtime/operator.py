"""Trusted proxy identity and one-use audited effect/stop maintenance. 可信代理身份及一次性可审计效果/停止维护。"""

from __future__ import annotations

import hashlib
import ipaddress
import time
from dataclasses import dataclass
from uuid import uuid4

from embodied.contracts.effect import effect_digest
from embodied.contracts.operator import OperatorAuthorizationRequest, OperatorConfirmation, OperatorPolicy
from embodied.runtime.recovery import review_state


class OperatorDenied(RuntimeError):
    pass


class OperatorForbidden(OperatorDenied):
    """Authenticated subject lacks the requested policy scope. 已认证主体缺少所请求策略作用域。"""


@dataclass(frozen=True)
class OperatorPrincipal:
    subject_hash: str
    policy_digest: str
    scopes: tuple[str, ...] = ("reconcile_effect",)
    issuer: str = "tailscale_serve"


class OperatorAuthority:
    def __init__(self, registry, policy: OperatorPolicy, *, clock=time.monotonic):
        self.registry = registry
        self.policy = policy
        body = policy.model_dump(mode="json")
        body["subjects"] = sorted(body["subjects"])
        body["scopes"] = sorted(body["scopes"])
        self.policy_digest = effect_digest(body)
        self._scopes = tuple(body["scopes"])
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
        return OperatorPrincipal(hashlib.sha256(values[0].encode()).hexdigest(), self.policy_digest, self._scopes)

    def _principal(self, principal, *, scope: str | None = None):
        subjects = {hashlib.sha256(s.encode()).hexdigest() for s in self.policy.subjects}
        if (not isinstance(principal, OperatorPrincipal) or principal.issuer != "tailscale_serve"
                or principal.policy_digest != self.policy_digest or principal.subject_hash not in subjects
                or principal.scopes != self._scopes):
            raise OperatorDenied("invalid_operator_principal")
        if scope is not None and scope not in self.policy.scopes:
            raise OperatorForbidden("operator_scope_not_allowed")

    def _target(self, request: OperatorAuthorizationRequest, report):
        registry = self.registry
        review = report.get("recovery_review", {})
        if not review.get("coherent") or report.get("workflow") in {"LEGACY", "CORRUPT"}:
            raise OperatorDenied("review_context_unavailable")
        step = next((s for s in report.get("steps", []) if s["operation_id"] == request.operation_id), None)
        record = registry.ledger.get(request.operation_id)
        if (step is None or record is None or record.task_id != request.task_id
                or record.attempt_id != request.attempt_id or step["attempt_id"] != request.attempt_id
                or step["call_state"] == "IDENTITY_MISMATCH"
                or not step.get("intent") or step["intent"]["request_digest"] != record.request_digest):
            raise OperatorDenied("review_target_mismatch")
        controller = registry._execution_controller
        if controller is None or controller.sim.guard.limits is not controller._limits:
            raise OperatorDenied("guard_unavailable")
        if request.scope == "reconcile_effect":
            if (not report.get("context_matches") or "guard_unavailable" in review.get("blockers", [])
                    or controller.sim.guard.halted):
                raise OperatorDenied("guard_unavailable" if controller.sim.guard.halted else "review_context_unavailable")
            if not step["manifest_matches"]:
                raise OperatorDenied("review_target_mismatch")
            # 使用现有维护准入核对停止、上下文及资源；此处不观察、不写入。
            # Reuse maintenance admission for stop/context/resources; do not observe or write here.
            _, _, lease = registry._reserve_effect(record.operation_id, record.attempt_id)
            registry.arbiter.release(lease)
            return record
        # 停止对账允许跨重启与 Guard 闩锁状态，但绝不放宽物理停止准入与隔离归属校验。
        # Stop reconciliation allows cross-restart and latched Guard states, without relaxing stop admission or quarantine ownership.
        registry._validate_stop_reconciliation(
            record.operation_id, record.attempt_id, allow_already_observed=False, check_lock=True,
        )
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
        first_v3_write = self.registry.ledger.schema_version == 3 and db.execute("SELECT v3_dirty FROM schema_version").fetchone()[0] == 0
        self._audit(record, kind, payload)
        # 仅允许本次审计行与首次 v3 写标志。 / Allow only this audit row and the first v3 write marker.
        if (review_state(self.registry, report, include_storage=False)[0] != before
                or db.total_changes != changes + 1 + int(first_v3_write) or db.execute("PRAGMA data_version").fetchone()[0] != version):
            raise OperatorDenied("review_snapshot_changed")

    async def issue(self, principal: OperatorPrincipal, request: OperatorAuthorizationRequest):
        self.registry.check_thread()
        self._principal(principal, scope=request.scope)
        if request.scope == "retry_step":
            return await self.registry.step_retries.issue(principal, request)
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
        if request.scope == "reconcile_stop":
            audit.update(
                stop_profile_digest=self.registry._stop_controller.profile.digest,
                guard_halted=bool(self.registry._execution_controller.sim.guard.halted),
            )
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
        if self.registry.step_retries.recognizes(confirmation.challenge_id):
            self._principal(principal, scope="retry_step")
            return await self.registry.step_retries.consume(principal, confirmation)
        pending = self._pending.get(confirmation.challenge_id)
        if pending is None or pending["principal"] != principal:
            raise OperatorDenied("operator_challenge_unavailable")
        # 确认一旦提交即烧毁凭据，存储失败也不能重用。 / Burn on confirmation submission, even if persistence fails.
        self._pending.pop(confirmation.challenge_id)
        request = pending["request"]
        self._principal(principal, scope=request.scope)
        if self._clock() >= pending["deadline"]:
            raise OperatorDenied("operator_challenge_expired")
        if review_state(self.registry, pending["report"])[0] != pending["state"]:
            raise OperatorDenied("review_snapshot_changed")
        record = self._target(request, pending["report"])
        audit = dict(pending["audit"], confirmed=confirmation.confirmed)
        self._audit_bound(record, "operator_authorization_consumed", audit, pending["report"])
        if self._clock() >= pending["deadline"]:
            raise OperatorDenied("operator_challenge_expired")
        if not confirmation.confirmed:
            self._audit(record, "operator_authorization_finished", dict(audit, status="declined"))
            return {"status": "declined", "task_execution_authorized": False}
        if request.scope == "reconcile_effect":
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
        try:
            updated = await self.registry.reconcile_stop(
                record.operation_id, record.attempt_id, confirmed=True, allow_already_observed=False,
            )
        except BaseException as exc:
            try:
                self._audit(record, "operator_authorization_finished", dict(audit, status="failed", error=type(exc).__name__))
            except Exception:
                pass
            raise
        guard_halted = bool(self.registry._execution_controller.sim.guard.halted)
        self._audit(record, "operator_authorization_finished", dict(
            audit, status="completed", stop_state=updated.stop_state,
            stop_nonce=updated.stop_evidence["nonce"], stop_profile_digest=updated.stop_evidence["profile_digest"],
            gripper_strategy=updated.stop_evidence["gripper_strategy"], guard_halted=guard_halted,
        ))
        return {"status": "completed", "challenge_id": confirmation.challenge_id, "scope": "reconcile_stop",
                "operation_id": record.operation_id, "attempt_id": record.attempt_id,
                "invocation_state": updated.state, "stop_state": updated.stop_state,
                "stop_nonce": updated.stop_evidence["nonce"],
                "gripper_strategy": updated.stop_evidence["gripper_strategy"],
                "guard_halted": guard_halted, "task_execution_authorized": False}
