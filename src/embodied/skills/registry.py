"""SkillRegistry: the only gate through which the planner reaches the physical world.

Two invariants are pinned by contract tests and must never regress:
1. Adding a skill = register(manifest, handler); the core is never edited for it.
2. require_confirm skills cannot execute without confirmed=True — the gate lives HERE,
   below the planner, so a planner bug cannot bypass it (SG-3, docs/architecture.md §4.5).


Registry 保证新增技能只注册即可扩展，并在规划器下方强制危险技能确认；两项契约不可回退。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
import time
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable
from uuid import uuid4

from embodied.contracts.effect import EFFECT_MAX_AGE_S, EffectRejected, effect_digest
from embodied.contracts.operation import OperationIdentity
from embodied.contracts.stopping import StopEvidence, StopProfile, StopRejected, StopRequest, validate_stop_evidence
from embodied.runtime.arbiter import Lease, ResourceArbiter, ResourceBusy
from embodied.runtime.invocation import CURRENT_INVOCATION, InvocationPermit
from embodied.runtime.operations import OperationLedger, OperationRecord, request_digest
from embodied.skills.manifest import SkillManifest


class SkillError(RuntimeError):
    pass


class SkillNotFound(SkillError):
    pass


class DuplicateSkill(SkillError):
    pass


class InvalidParams(SkillError):
    pass


class ConfirmationRequired(SkillError):
    def __init__(self, name: str):
        super().__init__(f"skill {name} requires explicit confirmation")
        self.skill = name


@dataclass
class SkillResult:
    ok: bool
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    operation_id: str = ""
    attempt_id: str = ""
    invocation_state: str = ""
    stop_state: str = ""


SkillHandler = Callable[..., Awaitable[SkillResult]]


@dataclass
class _Dispatch:
    record: OperationRecord
    permit: InvocationPermit
    lease: Lease


class SkillRegistry:
    def __init__(self, *, ledger: OperationLedger | None = None, robot_id: str = "reference") -> None:
        self._skills: dict[str, tuple[SkillManifest, SkillHandler]] = {}
        self._retiring: set[asyncio.Task] = set()
        self.ledger = ledger if ledger is not None else OperationLedger()
        self.robot_id = robot_id
        self.arbiter = ResourceArbiter(self.ledger.quarantined())
        self._active: dict[str, tuple[asyncio.Task, InvocationPermit, Lease]] = {}
        self._tracked_skills: set[str] = set()
        self._stop_contexts: dict[str, InvocationPermit] = {}
        self._stop_controller = None
        self._stop_lock = asyncio.Lock()
        self._stop_inflight: asyncio.Task | None = None
        self._execution_controller = None
        self._closed = False
        self._thread_id = threading.get_ident()
        self._effect_observer = None

    def configure_effect_observer(self, embodiment) -> None:
        self.check_thread()
        if embodiment.action_controller is not self._execution_controller:
            raise SkillError("effect observer controller mismatch")
        if self._effect_observer is not None and self._effect_observer.embodiment is not embodiment:
            raise SkillError("effect observer already bound")
        from embodied.runtime.effects import EffectObserver
        self._effect_observer = EffectObserver(embodiment)

    def check_thread(self) -> None:
        if threading.get_ident() != self._thread_id:
            raise SkillError("runtime admission must run on its owner thread")

    def configure_execution_controller(self, controller) -> None:
        self.check_thread()
        if self._execution_controller is not None and self._execution_controller is not controller:
            raise SkillError("runtime already bound to another controller")
        with controller.admission():
            if self.robot_id == "reference" and not self.ledger.has_operations():
                self.robot_id = controller.profile.robot_id
            if self.robot_id != controller.profile.robot_id:
                raise SkillError("controller robot identity mismatch")
            controller.bind(self)
            self._execution_controller = controller

    def register(self, manifest: SkillManifest, handler: SkillHandler, *, tracked_workers: bool = False) -> None:
        if manifest.name in self._skills:
            raise DuplicateSkill(manifest.name)
        if not inspect.iscoroutinefunction(handler):
            raise SkillError(f"handler for {manifest.name} must be an async function")
        self._skills[manifest.name] = (manifest, handler)
        if tracked_workers:
            self._tracked_skills.add(manifest.name)

    def configure_stop_controller(self, controller) -> None:
        if not isinstance(controller.profile, StopProfile):
            raise SkillError("invalid stop controller profile")
        if controller.profile.robot_id != self.robot_id:
            return
        if self._stop_controller is not None and self._stop_controller is not controller:
            raise SkillError("a different stop controller is already bound")
        self._stop_controller = controller

    def get(self, name: str) -> SkillManifest:
        try:
            return self._skills[name][0]
        except KeyError:
            raise SkillNotFound(name) from None

    def catalog(self) -> list[SkillManifest]:
        return [m for m, _ in sorted(self._skills.values(), key=lambda t: t[0].name)]

    def tool_schemas(self) -> list[dict[str, Any]]:
        return [m.to_tool_schema() for m in self.catalog()]

    def resolve_tool(self, tool_name: str) -> str:
        name = tool_name.replace("-", ".")
        if name not in self._skills:
            raise SkillNotFound(tool_name)
        return name

    async def invoke(
        self, name: str, params: dict[str, Any] | None = None, *, confirmed: bool = False,
        operation: OperationIdentity | None = None,
    ) -> SkillResult:
        try:
            manifest, handler = self._skills[name]
        except KeyError:
            raise SkillNotFound(name) from None
        merged = self._parameters(manifest, params, confirmed)
        with self._dispatch(manifest, merged, operation, "skill", name in self._tracked_skills) as dispatch:
            if isinstance(dispatch, SkillResult):
                return dispatch
            result = await self._execute(manifest, handler, merged, dispatch.permit)
            return self._finish(dispatch, result)

    def invoke_operator(self, manifest: SkillManifest, handler, params: dict | None = None, *,
                        operation: OperationIdentity | None = None, confirmed: bool = False) -> SkillResult:
        """Synchronous operator admission, excluded from the model catalog.

        同步操作员准入，不加入模型工具目录。
        """
        merged = self._parameters(manifest, params, confirmed)
        with self._dispatch(manifest, merged, operation, "teleop", True) as dispatch:
            if isinstance(dispatch, SkillResult):
                return dispatch
            binding = CURRENT_INVOCATION.set(dispatch.permit)
            try:
                result = handler(**merged)
                with dispatch.permit.access() as allowed:
                    if not allowed:
                        result = SkillResult(ok=False, detail=dispatch.permit.rejection_reason or "operator timeout",
                                             data={"_execution": {"outcome": "OUTCOME_UNKNOWN"}})
                return self._finish(dispatch, result)
            finally:
                CURRENT_INVOCATION.reset(binding)

    def _parameters(self, manifest, params, confirmed):
        self.check_thread()
        if self._closed:
            raise SkillError("runtime is closed")
        if manifest.require_confirm and confirmed is not True:
            raise ConfirmationRequired(manifest.name)
        return self._validate_params(manifest, {} if params is None else params)

    @contextmanager
    def _dispatch(self, manifest, params, operation, source, tracked):
        identity = operation or OperationIdentity.fresh()
        controller = self._execution_controller
        with controller.admission() if controller is not None else nullcontext():
            declaration = manifest.model_dump(mode="json")
            if source != "skill":
                declaration["execution_source"] = source
            digest = request_digest(manifest.name, params, declaration, self.robot_id)
            previous = self.ledger.lookup(identity, digest)
            if previous:
                outcome = self._from_record(previous)
                if manifest.verification.get("mode") == "state_predicate":
                    outcome.data["_operation_replay"] = True
            else:
                try:
                    lease = self.arbiter.acquire(identity.operation_id, self.ledger.next_epoch(), manifest.resources)
                except ResourceBusy as exc:
                    outcome = SkillResult(ok=False, detail=str(exc), operation_id=identity.operation_id,
                                          invocation_state="REJECTED")
                else:
                    try:
                        stop = self._stop_controller
                        context = {"tracked_workers": tracked, "stop_profile": None, "execution_source": source}
                        if self._effect_observer is not None and manifest.verification.get("mode") == "state_predicate":
                            context["effect_context"] = self._effect_observer.context(manifest.verification, params)
                        if stop is not None and set(lease.resources) == set(stop.profile.resources):
                            context["stop_profile"] = stop.profile.model_dump(mode="json")
                        record = self.ledger.begin(identity, self.robot_id, manifest.name, digest,
                                                   lease.resources, lease.epoch, stop_context=context)
                    except BaseException:
                        self.arbiter.release(lease)
                        raise
                    permit = InvocationPermit(manifest.termination.timeout_s, lambda: self.arbiter.valid(lease))
                    permit.issuer = self
                    permit.operation_id, permit.attempt_id = record.operation_id, record.attempt_id
                    permit.runtime_boot_id, permit.lease_epoch = self.ledger.boot_id, lease.epoch
                    permit.resources, permit.source = lease.resources, source
                    if controller is not None:
                        permit.scene_epoch, permit.control_epoch = controller.sim.state_epoch, controller.epoch
                    self._stop_contexts[identity.operation_id] = permit
                    try:
                        task = asyncio.current_task()
                    except RuntimeError:
                        task = None
                    self._active[identity.operation_id] = (task, permit, lease)
                    outcome = _Dispatch(record, permit, lease)
        if isinstance(outcome, SkillResult):
            yield outcome
            return
        try:
            yield outcome
        except BaseException:
            outcome.permit.revoke()
            self.arbiter.quarantine(outcome.lease)
            self.ledger.finish(identity.operation_id, "OUTCOME_UNKNOWN", None, "STOP_UNCONFIRMED")
            raise
        finally:
            outcome.permit.revoke()
            self._active.pop(identity.operation_id, None)
            if outcome.record.state in {"RETURNED", "FAILED"}:
                self._stop_contexts.pop(identity.operation_id, None)
                self.arbiter.release(outcome.lease)
            else:
                self.arbiter.quarantine(outcome.lease)

    def _finish(self, dispatch, result):
        permit = dispatch.permit
        result.data.pop("_effect", None)
        result.data.pop("_operation_replay", None)
        unknown = result.data.get("_execution", {}).get("outcome") == "OUTCOME_UNKNOWN"
        if permit.rejection_reason or not permit.drained:
            if not unknown:
                result = SkillResult(ok=False, detail=permit.rejection_reason or "tracked work unfinished")
            unknown = True
        if unknown:
            result.ok = False
            result.data["_execution"] = {"outcome": "OUTCOME_UNKNOWN", "stop_state": "STOP_UNCONFIRMED"}
        state = "OUTCOME_UNKNOWN" if unknown else "RETURNED" if result.ok else "FAILED"
        dispatch.record = self.ledger.finish(dispatch.record.operation_id, state, asdict(result),
                                             "STOP_UNCONFIRMED" if unknown else "NOT_REQUESTED")
        if self._effect_observer is not None and "effect_context" in dispatch.record.stop_context:
            unavailable = "call_outcome_unknown" if unknown else ""
            if not set(self._effect_observer.controller.profile.resources).issubset(dispatch.record.resources):
                unavailable = "effect_resources_not_owned"
            evidence = self._effect_observer.capture(
                dispatch.record, self.ledger.boot_id, unavailable=unavailable,
            )
            dispatch.record = self.ledger.record_effect(evidence)
        return self._from_record(dispatch.record)

    @staticmethod
    def _from_record(record: OperationRecord) -> SkillResult:
        if record.result is not None:
            result = SkillResult(**record.result)
        else:
            result = SkillResult(ok=False, detail=f"operation {record.state}; not re-executed",
                                 data={"_execution": {"outcome": "OUTCOME_UNKNOWN",
                                                       "stop_state": record.stop_state}})
        result.operation_id = record.operation_id
        result.attempt_id = record.attempt_id
        result.invocation_state = record.state
        result.stop_state = record.stop_state
        result.data.pop("_effect", None)
        if record.effect_evidence is not None:
            result.data["_effect"] = dict(record.effect_evidence)
        elif "effect_context" in record.stop_context:
            result.data["_effect"] = {"evidence_id": ""}
        if record.state == "OUTCOME_UNKNOWN":
            result.ok = False
            result.data["_execution"] = {"outcome": "OUTCOME_UNKNOWN", "stop_state": record.stop_state}
        return result

    def check_effect(self, operation_id: str, attempt_id: str, evidence_id: str,
                     verification_digest: str) -> dict:
        self.check_thread()
        record = self.ledger.get(operation_id)
        answer = {"eligible": False, "verdict": "unknown", "age_s": 0.0,
                  "reason": "effect_unavailable", "evidence": None}
        if record is None or record.attempt_id != attempt_id or record.effect_evidence is None:
            return answer
        evidence = record.effect_evidence
        answer["evidence"] = evidence
        age = time.monotonic() - evidence["captured_monotonic"]
        if (evidence["evidence_id"] != evidence_id or evidence["verification_digest"] != verification_digest
                or evidence["observer_boot_id"] != self.ledger.boot_id or age < 0 or age >= EFFECT_MAX_AGE_S):
            answer["reason"] = "effect_identity_or_age_mismatch"
            return answer
        if (self._effect_observer is None or not self._effect_observer.matches(record.stop_context["effect_context"])
                or self.ledger.newer_conflict(record)
                or not self.arbiter.idle(self._effect_observer.controller.profile.resources)
                or not self._effect_observer.evidence_current(evidence)):
            answer["reason"] = "effect_context_or_owner_changed"
            return answer
        answer.update(eligible=True, verdict=evidence["verdict"], age_s=age, reason=evidence["reason"])
        return answer

    def effect_verdict(self, operation_id: str, attempt_id: str, evidence_id: str, verification: dict) -> str:
        return self.check_effect(operation_id, attempt_id, evidence_id, effect_digest(verification))["verdict"]

    def reconcile_effect(self, operation_id: str, attempt_id: str) -> OperationRecord:
        self.check_thread()
        record = self.ledger.get(operation_id)
        if record is None or record.attempt_id != attempt_id or record.state == "RUNNING":
            raise EffectRejected("effect_attempt_unavailable")
        observer = self._effect_observer
        if observer is None or "effect_context" not in record.stop_context:
            raise EffectRejected("effect_context_unavailable")
        if (operation_id in self._active or any(not permit.drained for permit in self._stop_contexts.values())
                or record.state == "OUTCOME_UNKNOWN" and record.stop_state != "MOTION_STOPPED_OBSERVED"):
            raise EffectRejected("effect_stop_unconfirmed")
        with observer.controller.admission():
            if not self.arbiter.idle(observer.controller.profile.resources):
                raise EffectRejected("effect_resources_busy")
            if not observer.matches(record.stop_context["effect_context"]) or self.ledger.newer_conflict(record):
                raise EffectRejected("effect_context_or_owner_changed")
            return self.ledger.record_effect(observer.capture(record, self.ledger.boot_id))

    def get_operation(self, operation_id: str) -> OperationRecord | None:
        return self.ledger.get(operation_id)

    def cancel_operation(self, operation_id: str) -> OperationRecord | None:
        self.check_thread()
        active = self._active.get(operation_id)
        if active:
            task, permit, lease = active
            permit.revoke()
            self.arbiter.quarantine(lease)
            record = self.ledger.finish(operation_id, "OUTCOME_UNKNOWN", None, "STOP_UNCONFIRMED",
                                        event="cancel_requested")
            if task is not None:
                task.cancel()
            return record
        return self.ledger.get(operation_id)

    def cancel_all(self) -> None:
        for operation_id in list(self._active):
            self.cancel_operation(operation_id)

    def close(self) -> None:
        self.check_thread()
        if (self._active or any(not permit.drained for permit in self._stop_contexts.values())
                or self._stop_inflight is not None and not self._stop_inflight.done()):
            raise SkillError("cannot close registry with active operations or workers")
        self.ledger.close()
        self._closed = True

    async def reconcile_stop(self, operation_id: str, attempt_id: str, *, confirmed: bool = False) -> OperationRecord:
        self.check_thread()
        if confirmed is not True:
            raise ConfirmationRequired("maintenance.reconcile_stop")
        async with self._stop_lock:
            record = self.ledger.get(operation_id)
            if record is None or record.attempt_id != attempt_id:
                raise StopRejected("stop_attempt_mismatch")
            if record.state != "OUTCOME_UNKNOWN":
                raise StopRejected("operation_not_unknown")
            if record.stop_state == "MOTION_STOPPED_OBSERVED":
                return record
            if record.robot_id != self.robot_id:
                raise StopRejected("stop_robot_mismatch")
            if operation_id in self._active:
                raise StopRejected("invocation_still_active")
            permit = self._stop_contexts.get(operation_id)
            if record.boot_id == self.ledger.boot_id and permit is None:
                raise StopRejected("stop_worker_identity_unavailable")
            if permit is not None and not permit.drained:
                raise StopRejected("worker_still_running")
            if record.stop_context.get("tracked_workers") is not True:
                raise StopRejected("stop_tracking_unavailable")
            controller = self._stop_controller
            if controller is None:
                raise StopRejected("stop_controller_unavailable")
            try:
                profile = StopProfile.model_validate_json(json.dumps(record.stop_context["stop_profile"]))
            except (ValueError, TypeError, KeyError):
                raise StopRejected("stop_profile_unavailable") from None
            if profile.digest != controller.profile.digest:
                raise StopRejected("stop_profile_changed")
            if self._stop_inflight is not None and not self._stop_inflight.done():
                raise StopRejected("stop_observer_still_running")
            request = StopRequest(
                operation_id=operation_id, attempt_id=attempt_id, attempt_boot_id=record.boot_id,
                observer_boot_id=self.ledger.boot_id, robot_id=self.robot_id, lease_epoch=record.lease_epoch,
                resources=record.resources, nonce=uuid4().hex,
                deadline_monotonic=time.monotonic() + profile.max_wall_duration_s,
            )
            self.ledger.note_stop_request(request, profile)
            task = asyncio.create_task(asyncio.to_thread(controller.prepare, request))
            self._stop_inflight = task
            task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
            try:
                evidence = await asyncio.wait_for(asyncio.shield(task), timeout=profile.max_wall_duration_s + 0.1)
                if not isinstance(evidence, StopEvidence):
                    raise StopRejected("invalid_stop_evidence")
                validate_stop_evidence(evidence, request, profile)
                result = controller.commit(
                    evidence, request,
                    lambda: self.arbiter.resolve(operation_id, record.resources,
                                                lambda: self.ledger.record_stop(evidence)),
                )
            except asyncio.TimeoutError:
                raise StopRejected("stop_observation_timeout") from None
            except (ValueError, TypeError):
                raise StopRejected("invalid_stop_evidence") from None
            self._stop_contexts.pop(operation_id, None)
            return result

    async def _execute(
        self, manifest: SkillManifest, handler: SkillHandler, merged: dict, permit: InvocationPermit,
    ) -> SkillResult:
        binding = CURRENT_INVOCATION.set(permit)
        task = None
        try:
            task = asyncio.create_task(handler(**merged))
            permit.handler_task = task
            done, _ = await asyncio.wait({task}, timeout=manifest.termination.timeout_s)
            with permit.access() as allowed:
                timed_out = not done or not allowed
            if timed_out:
                return SkillResult(
                    ok=False, detail=permit.rejection_reason or f"timeout after {manifest.termination.timeout_s}s",
                    data={"_execution": {"outcome": "OUTCOME_UNKNOWN", "stop_state": "STOP_UNCONFIRMED"}},
                )
            return task.result()
        except SkillError:
            raise
        # handler 异常视为操作失败，不使系统崩溃。
        except Exception as e:  # handler faults are operational failures, not crashes
            return SkillResult(ok=False, detail=f"{type(e).__name__}: {e}",
                               data={"_execution": {"outcome": "OUTCOME_UNKNOWN", "stop_state": "STOP_UNCONFIRMED"}})
        finally:
            # 先撤销许可再取消协程，因为线程任务可能仍在运行。
            # Revoke BEFORE coroutine cancellation; to_thread work may continue running.
            permit.revoke()
            CURRENT_INVOCATION.reset(binding)
            if task is not None and not task.done():
                task.cancel()
                self._retiring.add(task)
                task.add_done_callback(self._retired)

    def _retired(self, task: asyncio.Task) -> None:
        self._retiring.discard(task)
        if not task.cancelled():
            # 消费迟到异常，不恢复调用权限。
            task.exception()  # consume late handler faults without reviving the invocation

    @staticmethod
    def _validate_params(manifest: SkillManifest, params: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(params, dict) or not all(isinstance(key, str) for key in params):
            raise InvalidParams(f"{manifest.name}: params must be an object with string keys")
        unknown = set(params) - set(manifest.params)
        if unknown:
            raise InvalidParams(f"{manifest.name}: unknown params {sorted(unknown)}")
        merged: dict[str, Any] = {}
        for pname, spec in manifest.params.items():
            if pname in params:
                merged[pname] = params[pname]
            elif spec.default is not None:
                merged[pname] = spec.default
            elif spec.required:
                raise InvalidParams(f"{manifest.name}: missing required param {pname!r}")
            if pname in merged:
                try:
                    spec.validate_value(merged[pname])
                except (ValueError, TypeError, RecursionError) as exc:
                    raise InvalidParams(f"{manifest.name}: invalid param {pname!r}: {exc}") from exc
        return merged
