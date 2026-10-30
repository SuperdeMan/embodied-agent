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
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable
from uuid import uuid4

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
        if manifest.require_confirm and confirmed is not True:
            raise ConfirmationRequired(name)
        merged = self._validate_params(manifest, {} if params is None else params)
        identity = operation or OperationIdentity.fresh()
        digest = request_digest(name, merged, manifest.model_dump(mode="json"), self.robot_id)
        previous = self.ledger.lookup(identity, digest)
        if previous:
            return self._from_record(previous)
        try:
            lease = self.arbiter.acquire(identity.operation_id, self.ledger.next_epoch(), manifest.resources)
        except ResourceBusy as exc:
            return SkillResult(ok=False, detail=str(exc), operation_id=identity.operation_id,
                               invocation_state="REJECTED")
        try:
            controller = self._stop_controller
            stop_context = {"tracked_workers": name in self._tracked_skills, "stop_profile": None}
            if controller is not None and set(lease.resources) == set(controller.profile.resources):
                stop_context["stop_profile"] = controller.profile.model_dump(mode="json")
            record = self.ledger.begin(identity, self.robot_id, name, digest, lease.resources, lease.epoch,
                                       stop_context=stop_context)
        except BaseException:
            self.arbiter.release(lease)
            raise
        permit = InvocationPermit(manifest.termination.timeout_s, lambda: self.arbiter.valid(lease))
        self._stop_contexts[identity.operation_id] = permit
        self._active[identity.operation_id] = (asyncio.current_task(), permit, lease)
        try:
            result = await self._execute(manifest, handler, merged, permit)
            unknown = result.data.get("_execution", {}).get("outcome") == "OUTCOME_UNKNOWN"
            if not permit.drained:
                if not unknown:
                    result = SkillResult(ok=False, detail="handler exited before tracked work finished")
                unknown = True
            if unknown:
                result.ok = False
                result.data["_execution"] = {"outcome": "OUTCOME_UNKNOWN", "stop_state": "STOP_UNCONFIRMED"}
            state = "OUTCOME_UNKNOWN" if unknown else "RETURNED" if result.ok else "FAILED"
            stopped = "STOP_UNCONFIRMED" if unknown else "NOT_REQUESTED"
            record = self.ledger.finish(identity.operation_id, state, asdict(result), stopped)
            return self._from_record(record)
        except BaseException:
            permit.revoke()
            self.arbiter.quarantine(lease)
            self.ledger.finish(identity.operation_id, "OUTCOME_UNKNOWN", None, "STOP_UNCONFIRMED")
            raise
        finally:
            permit.revoke()
            self._active.pop(identity.operation_id, None)
            if record.state in {"RETURNED", "FAILED"}:
                self._stop_contexts.pop(identity.operation_id, None)
                self.arbiter.release(lease)
            else:
                self.arbiter.quarantine(lease)

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
        if record.state == "OUTCOME_UNKNOWN":
            result.ok = False
            result.data["_execution"] = {"outcome": "OUTCOME_UNKNOWN", "stop_state": record.stop_state}
        return result

    def get_operation(self, operation_id: str) -> OperationRecord | None:
        return self.ledger.get(operation_id)

    def cancel_operation(self, operation_id: str) -> OperationRecord | None:
        active = self._active.get(operation_id)
        if active:
            task, permit, lease = active
            permit.revoke()
            self.arbiter.quarantine(lease)
            record = self.ledger.finish(operation_id, "OUTCOME_UNKNOWN", None, "STOP_UNCONFIRMED",
                                        event="cancel_requested")
            task.cancel()
            return record
        return self.ledger.get(operation_id)

    def cancel_all(self) -> None:
        for operation_id in list(self._active):
            self.cancel_operation(operation_id)

    def close(self) -> None:
        if (self._active or any(not permit.drained for permit in self._stop_contexts.values())
                or self._stop_inflight is not None and not self._stop_inflight.done()):
            raise SkillError("cannot close registry with active operations or workers")
        self.ledger.close()

    async def reconcile_stop(self, operation_id: str, attempt_id: str, *, confirmed: bool = False) -> OperationRecord:
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
                    ok=False, detail=f"timeout after {manifest.termination.timeout_s}s",
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
