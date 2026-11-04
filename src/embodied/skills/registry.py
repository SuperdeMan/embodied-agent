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
import logging
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
from embodied.runtime.observation import OBSERVATION_TIMEOUT_S, ObservationLane, ObservationUnavailable
from embodied.runtime.operations import OperationLedger, OperationRecord, request_digest
from embodied.skills.manifest import SkillManifest

logger = logging.getLogger("skills.registry")


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
    def __init__(self, *, ledger: OperationLedger | None = None, robot_id: str = "reference",
                 observation_timeout_s: float = OBSERVATION_TIMEOUT_S) -> None:
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
        self._observations = ObservationLane(observation_timeout_s)
        self._read_barriers: list[Callable[[], bool]] = []
        self._preview_admissions: dict[str, asyncio.Task] = {}
        self._compute_providers: list[Any] = []

    def add_compute_provider(self, provider) -> None:
        self.check_thread()
        if callable(getattr(provider, "close", None)) and all(item is not provider for item in self._compute_providers):
            self._compute_providers.append(provider)

    def restart_compute(self) -> None:
        self.check_thread()
        if self._execution_controller is None:
            raise SkillError("compute controller unavailable")
        self._execution_controller.require_idle()
        for provider in self._compute_providers:
            if getattr(getattr(provider, "worker", None), "state", None) == "FAILED":
                provider.restart()
        sim = self._execution_controller.sim
        if getattr(getattr(sim, "_render_process", None), "state", None) == "FAILED":
            sim.restart_renderer()

    async def drain_compute(self, timeout_s: float = 3.0) -> None:
        self.check_thread()
        self.cancel_all()
        for provider in self._compute_providers:
            worker = getattr(provider, "worker", None)
            if worker is not None and worker.busy:
                worker.request_stop()
        if self._execution_controller is not None:
            cancel_render = getattr(self._execution_controller.sim, "cancel_render", None)
            if cancel_render is not None:
                cancel_render()
        deadline = time.monotonic() + timeout_s
        while (self.observation_pending or self._active or any(not p.drained for p in self._stop_contexts.values())
               or self._stop_inflight is not None and not self._stop_inflight.done()):
            if time.monotonic() >= deadline:
                raise SkillError("compute_shutdown_incomplete")
            await asyncio.sleep(0.005)

    @property
    def observation_pending(self) -> bool:
        return bool(self._preview_admissions) or self._observations.busy or any(barrier() for barrier in self._read_barriers)

    def add_read_barrier(self, barrier: Callable[[], bool]) -> None:
        self.check_thread()
        self._read_barriers.append(barrier)

    async def observe(self, function, *args, **kwargs):
        self.check_thread()
        if self._closed:
            raise ObservationUnavailable("runtime_closed")
        return await self._observations.call(function, *args, **kwargs)

    def record_task_plan(self, payload):
        self.check_thread()
        from embodied.runtime.task_journal import record_plan
        return record_plan(self, payload)

    def record_task_intent(self, task_id, revision, step_id, params):
        self.check_thread()
        from embodied.runtime.task_journal import record_intent
        record_intent(self, task_id, revision, step_id, params)

    def record_task_result(self, task_id, revision, step_id, result):
        self.check_thread()
        from embodied.runtime.task_journal import record_result
        record_result(self, task_id, revision, step_id, result)

    def finish_task(self, task_id, *, interrupted=False):
        self.check_thread()
        from embodied.runtime.task_journal import finish_task
        finish_task(self, task_id, interrupted=interrupted)

    def get_task(self, task_id):
        self.check_thread()
        from embodied.runtime.task_journal import report_task
        return report_task(self, task_id)

    def list_tasks(self, limit=20):
        self.check_thread()
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("task list limit must be in [1,50]")
        ids = [row[0] for row in self.ledger._db.execute("SELECT task_id FROM tasks ORDER BY updated_at DESC,task_id LIMIT ?", (limit,))]
        result = []
        for task_id in ids:
            report = self.get_task(task_id)
            result.append({key: report[key] for key in ("task_id", "robot_id", "workflow", "automatic_resume_allowed")})
        return result

    async def reconcile_task(self, task_id):
        from embodied.runtime.recovery import assess_task
        return await assess_task(self, task_id)

    def configure_effect_observer(self, embodiment) -> None:
        self.check_thread()
        if embodiment.action_controller is not self._execution_controller:
            raise SkillError("effect observer controller mismatch")
        if self._effect_observer is not None and self._effect_observer.embodiment is not embodiment:
            raise SkillError("effect observer already bound")
        from embodied.runtime.effects import EffectObserver
        self._effect_observer = EffectObserver(embodiment)
        provider = getattr(embodiment, "compute_provider", None)
        if provider is not None:
            self.add_compute_provider(provider)

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
        identity = operation or OperationIdentity.fresh()
        await self._await_preview(identity)
        with self._dispatch(manifest, merged, identity, "skill", name in self._tracked_skills) as dispatch:
            if isinstance(dispatch, SkillResult):
                return dispatch
            try:
                await self._bind_effect_async(dispatch)
            except Exception as exc:
                reason = str(exc) if isinstance(exc, ObservationUnavailable) else "effect_binding_failed"
                return await self._finish_async(dispatch, SkillResult(ok=False, detail=reason), unavailable=reason)
            result = await self._execute(manifest, handler, merged, dispatch.permit)
            return await self._finish_async(dispatch, result)

    async def _await_preview(self, identity):
        if (self.ledger.get(identity.operation_id) is not None or self._preview_admissions
                or self._observations.busy or not any(barrier() for barrier in self._read_barriers)):
            return
        # 仅允许一个准入请求等待当前帧；等待时禁止启动替代预览帧。
        # Only one admission may await the current frame; no replacement preview starts meanwhile.
        self._preview_admissions[identity.operation_id] = asyncio.current_task()
        deadline = time.monotonic() + self._observations.timeout_s
        try:
            while any(barrier() for barrier in self._read_barriers) and time.monotonic() < deadline:
                await asyncio.sleep(min(0.005, max(0.0, deadline - time.monotonic())))
        finally:
            self._preview_admissions.pop(identity.operation_id, None)

    def invoke_operator(self, manifest: SkillManifest, handler, params: dict | None = None, *,
                        operation: OperationIdentity | None = None, confirmed: bool = False) -> SkillResult:
        """Synchronous operator admission, excluded from the model catalog.

        同步操作员准入，不加入模型工具目录。
        """
        merged = self._parameters(manifest, params, confirmed)
        with self._dispatch(manifest, merged, operation, "teleop", True) as dispatch:
            if isinstance(dispatch, SkillResult):
                return dispatch
            try:
                context = dispatch.record.stop_context.get("effect_context", {})
                if context.get("binding_pending"):
                    context = self._observations.call_sync(self._effect_observer.bind_context, context)
                    self._commit_binding(dispatch, context)
            except Exception as exc:
                reason = str(exc) if isinstance(exc, ObservationUnavailable) else "effect_binding_failed"
                return self._finish(dispatch, SkillResult(ok=False, detail=reason), unavailable=reason)
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
        declaration = manifest.model_dump(mode="json")
        if source != "skill":
            declaration["execution_source"] = source
        digest = request_digest(manifest.name, params, declaration, self.robot_id)
        previous = self.ledger.lookup(identity, digest)
        if previous:
            outcome = self._from_record(previous)
            if manifest.verification.get("mode") == "state_predicate":
                outcome.data["_operation_replay"] = True
            yield outcome
            return
        from embodied.runtime.task_journal import validate_dispatch
        validate_dispatch(self, identity, digest)
        if self.observation_pending:
            yield SkillResult(ok=False, detail="observation_worker_busy", operation_id=identity.operation_id,
                              invocation_state="REJECTED")
            return
        controller = self._execution_controller
        lease = None
        with controller.admission() if controller is not None else nullcontext():
            try:
                lease = self.arbiter.acquire(identity.operation_id, self.ledger.next_epoch(), manifest.resources)
            except ResourceBusy as exc:
                outcome = SkillResult(ok=False, detail=str(exc), operation_id=identity.operation_id,
                                      invocation_state="REJECTED")
        if lease is not None:
            try:
                stop = self._stop_controller
                context = {"tracked_workers": tracked, "stop_profile": None, "execution_source": source}
                # 持资源租约采样，但不持原生锁等待渲染线程。 / Hold the lease, not the native lock, while awaiting rendering.
                if self._effect_observer is not None and manifest.verification.get("mode") == "state_predicate":
                    context["effect_context"] = self._effect_observer.context(manifest.verification, params)
                if stop is not None and set(lease.resources) == set(stop.profile.resources):
                    context["stop_profile"] = stop.profile.model_dump(mode="json")
                with controller.admission() if controller is not None else nullcontext():
                    if "effect_context" in context and not self._effect_observer.matches(context["effect_context"]):
                        raise EffectRejected("effect_context_changed")
                    record = self.ledger.begin(identity, self.robot_id, manifest.name, digest,
                                               lease.resources, lease.epoch, stop_context=context)
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
            except BaseException:
                self.arbiter.release(lease)
                raise
        if isinstance(outcome, SkillResult):
            yield outcome
            return
        try:
            yield outcome
        except BaseException:
            outcome.permit.revoke()
            if outcome.record.state not in {"RETURNED", "FAILED"}:
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

    async def _bind_effect_async(self, dispatch):
        context = dispatch.record.stop_context.get("effect_context", {})
        if context.get("binding_pending"):
            context = await self.observe(self._effect_observer.bind_context, context)
            self._commit_binding(dispatch, context)

    def _commit_binding(self, dispatch, context):
        with dispatch.permit.access() as allowed:
            if not allowed or not self._effect_observer.matches(context):
                raise EffectRejected("effect_binding_context_changed")
            dispatch.record = self.ledger.bind_effect(dispatch.record.operation_id, dispatch.record.attempt_id, context)

    def _store_call(self, dispatch, result, unavailable=""):
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
        if unknown:
            unavailable = "call_outcome_unknown"
        if self._effect_observer is not None and not set(self._effect_observer.controller.profile.resources).issubset(
            dispatch.record.resources
        ):
            unavailable = "effect_resources_not_owned"
        return unavailable

    def _unknown_effect(self, record, reason):
        return self._effect_observer.capture(record, self.ledger.boot_id, unavailable=reason)

    def _finish(self, dispatch, result, unavailable=""):
        unavailable = self._store_call(dispatch, result, unavailable)
        if self._effect_observer is not None and "effect_context" in dispatch.record.stop_context:
            try:
                evidence = (self._unknown_effect(dispatch.record, unavailable) if unavailable else
                            self._observations.call_sync(self._effect_observer.capture, dispatch.record, self.ledger.boot_id))
            except Exception as exc:
                reason = str(exc) if isinstance(exc, ObservationUnavailable) else "effect_observation_failed"
                evidence = self._unknown_effect(dispatch.record, reason)
            dispatch.record = self.ledger.record_effect(evidence)
        return self._from_record(dispatch.record)

    async def _finish_async(self, dispatch, result, unavailable=""):
        unavailable = self._store_call(dispatch, result, unavailable)
        if self._effect_observer is not None and "effect_context" in dispatch.record.stop_context:
            try:
                evidence = (self._unknown_effect(dispatch.record, unavailable) if unavailable else
                            await self.observe(self._effect_observer.capture, dispatch.record, self.ledger.boot_id))
            except asyncio.CancelledError:
                dispatch.record = self.ledger.record_effect(self._unknown_effect(dispatch.record, "effect_observation_cancelled"))
                raise
            except Exception as exc:
                reason = str(exc) if isinstance(exc, ObservationUnavailable) else "effect_observation_failed"
                evidence = self._unknown_effect(dispatch.record, reason)
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

    def _check_effect(self, operation_id: str, attempt_id: str, evidence_id: str,
                     verification_digest: str) -> dict:
        self.check_thread()
        record = self.ledger.get(operation_id)
        answer = {"eligible": False, "verdict": "unknown", "age_s": 0.0,
                  "reason": "effect_unavailable", "evidence": None}
        if self._observations.busy:
            answer["reason"] = "observation_worker_busy"
            return answer
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
                or not self.arbiter.idle(self._effect_observer.controller.profile.resources)):
            answer["reason"] = "effect_context_or_owner_changed"
            return answer
        answer.update(eligible=True, verdict=evidence["verdict"], age_s=age, reason=evidence["reason"])
        return answer

    @staticmethod
    def _unavailable_effect(answer, reason):
        return dict(answer, eligible=False, verdict="unknown", reason=reason)

    def check_effect(self, *args) -> dict:
        answer = self._check_effect(*args)
        if not answer["eligible"]:
            return answer
        try:
            current = self._observations.call_sync(self._effect_observer.evidence_current, answer["evidence"])
            return self._check_effect(*args) if current else self._unavailable_effect(answer, "effect_observation_stale")
        except Exception:
            return self._unavailable_effect(answer, "effect_check_unavailable")

    async def check_effect_async(self, *args) -> dict:
        answer = self._check_effect(*args)
        if not answer["eligible"]:
            return answer
        try:
            current = await self.observe(self._effect_observer.evidence_current, answer["evidence"])
            return self._check_effect(*args) if current else self._unavailable_effect(answer, "effect_observation_stale")
        except Exception:
            return self._unavailable_effect(answer, "effect_check_unavailable")

    def effect_verdict(self, operation_id: str, attempt_id: str, evidence_id: str, verification: dict) -> str:
        return self.check_effect(operation_id, attempt_id, evidence_id, effect_digest(verification))["verdict"]

    async def effect_verdict_async(self, operation_id: str, attempt_id: str, evidence_id: str, verification: dict) -> str:
        answer = await self.check_effect_async(operation_id, attempt_id, evidence_id, effect_digest(verification))
        return answer["verdict"]

    def _reserve_effect(self, operation_id: str, attempt_id: str):
        self.check_thread()
        if self.observation_pending:
            raise EffectRejected("observation_worker_busy")
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
            lease = self.arbiter.acquire("effect:" + uuid4().hex, self.ledger.next_epoch(), observer.controller.profile.resources)
        return record, observer, lease

    def _commit_effect(self, record, observer, evidence):
        if not observer.matches(record.stop_context["effect_context"]) or self.ledger.newer_conflict(record):
            raise EffectRejected("effect_context_or_owner_changed")
        return self.ledger.record_effect(evidence)

    def reconcile_effect(self, operation_id: str, attempt_id: str) -> OperationRecord:
        record, observer, lease = self._reserve_effect(operation_id, attempt_id)
        try:
            try:
                evidence = self._observations.call_sync(observer.capture, record, self.ledger.boot_id)
            except Exception as exc:
                reason = str(exc) if isinstance(exc, ObservationUnavailable) else "effect_observation_failed"
                evidence = self._unknown_effect(record, reason)
            return self._commit_effect(record, observer, evidence)
        finally:
            self.arbiter.release(lease)

    async def reconcile_effect_async(self, operation_id: str, attempt_id: str) -> OperationRecord:
        record, observer, lease = self._reserve_effect(operation_id, attempt_id)
        try:
            try:
                evidence = await self.observe(observer.capture, record, self.ledger.boot_id)
            except asyncio.CancelledError:
                self.ledger.record_effect(self._unknown_effect(record, "effect_observation_cancelled"))
                raise
            except Exception as exc:
                reason = str(exc) if isinstance(exc, ObservationUnavailable) else "effect_observation_failed"
                evidence = self._unknown_effect(record, reason)
            return self._commit_effect(record, observer, evidence)
        finally:
            self.arbiter.release(lease)

    def get_operation(self, operation_id: str) -> OperationRecord | None:
        return self.ledger.get(operation_id)

    def cancel_operation(self, operation_id: str) -> OperationRecord | None:
        self.check_thread()
        pending = self._preview_admissions.get(operation_id)
        if pending is not None:
            pending.cancel()
            return self.ledger.get(operation_id)
        active = self._active.get(operation_id)
        if active:
            task, permit, lease = active
            permit.revoke()
            # 撤权与协程取消不依赖账本读写成功。 / Revocation and cancellation do not depend on ledger I/O.
            if task is not None:
                task.cancel()
            try:
                record = self.ledger.get(operation_id)
                if record is None:
                    raise SkillError("active_operation_missing")
                if record.state == "RUNNING":
                    self.arbiter.quarantine(lease)
                    record = self.ledger.finish(operation_id, "OUTCOME_UNKNOWN", None, "STOP_UNCONFIRMED",
                                                event="cancel_requested")
                return record
            except Exception:
                self.arbiter.quarantine(lease)
                raise
        return self.ledger.get(operation_id)

    def cancel_all(self) -> dict[str, str]:
        self.check_thread()
        # 先撤销全部许可，避免第一项慢写或失败延迟其余调用的撤权。
        # Revoke every permit first so the first slow or failed write cannot delay other revocations.
        for task in self._preview_admissions.values():
            task.cancel()
        for task, permit, _ in self._active.values():
            permit.revoke()
            if task is not None:
                task.cancel()
        errors = {}
        for operation_id in list(self._preview_admissions) + list(self._active):
            try:
                self.cancel_operation(operation_id)
            except Exception as exc:
                errors[operation_id] = type(exc).__name__
                logger.error("cancellation persistence failed: operation=%s error=%s", operation_id, type(exc).__name__)
        return errors

    def close(self) -> None:
        self.check_thread()
        if (self.observation_pending or self._active or any(not permit.drained for permit in self._stop_contexts.values())
                or self._stop_inflight is not None and not self._stop_inflight.done()):
            raise SkillError("cannot close registry with active operations or workers")
        for provider in self._compute_providers:
            provider.close()
        self.ledger.close()
        self._closed = True

    async def reconcile_stop(self, operation_id: str, attempt_id: str, *, confirmed: bool = False) -> OperationRecord:
        self.check_thread()
        if confirmed is not True:
            raise ConfirmationRequired("maintenance.reconcile_stop")
        if self.observation_pending:
            raise StopRejected("observation_worker_still_running")
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
