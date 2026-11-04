"""RemoteSkillRegistry: agent-core's view of skills served by the realtime-control process.

Duck-types SkillRegistry for the planner engine (catalog/tool_schemas/resolve_tool/get/
invoke); manifests come from ListSkills, invocation streams SkillEvents until terminal.
The confirm UX stays in agent-core; the `confirmed` flag rides the SkillCommand — the
control process still re-checks require_confirm at its own registry (defense in depth).


远端注册表从控制服务获取 manifest 并流式调用至终态；Agent 负责确认交互，确认标记跨 RPC 传递，控制端再独立校验。
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import grpc

from embodied.contracts.effect import EFFECT_MAX_AGE_S, EffectEvidence, effect_digest
from embodied.contracts.observation import ObservationEnvelope
from embodied.contracts.operation import OperationIdentity
from embodied.contracts.task import TaskJournalError
from embodied.runtime.rpcgen import import_control
from embodied.runtime.world_state import SnapshotAdmission
from embodied.skills.manifest import SkillManifest
from embodied.skills.registry import ConfirmationRequired, SkillNotFound, SkillRegistry, SkillResult


class RemoteSkillRegistry:
    def __init__(self, channel: grpc.aio.Channel):
        _, pb2_grpc = import_control()
        self._pb2, _ = import_control()
        self._stub = pb2_grpc.ControlServiceStub(channel)
        self._manifests: dict[str, SkillManifest] = {}
        self._snapshot_admission = SnapshotAdmission()

    async def connect(self) -> "RemoteSkillRegistry":
        reply = await self._stub.ListSkills(self._pb2.ListSkillsRequest(), timeout=5.0)
        self._manifests = {}
        for info in reply.skills:
            m = SkillManifest.model_validate_json(info.manifest_json)
            self._manifests[m.name] = m
        return self

    # 技能注册表接口。
    # -- SkillRegistry surface -------------------------------------------------

    def get(self, name: str) -> SkillManifest:
        try:
            return self._manifests[name]
        except KeyError:
            raise SkillNotFound(name) from None

    def catalog(self) -> list[SkillManifest]:
        return [self._manifests[k] for k in sorted(self._manifests)]

    def tool_schemas(self) -> list[dict[str, Any]]:
        return [m.to_tool_schema() for m in self.catalog()]

    def resolve_tool(self, tool_name: str) -> str:
        name = tool_name.replace("-", ".")
        if name not in self._manifests:
            raise SkillNotFound(tool_name)
        return name

    async def invoke(
        self, name: str, params: dict[str, Any] | None = None, *, confirmed: bool = False,
        operation: OperationIdentity | None = None,
    ) -> SkillResult:
        manifest = self.get(name)
        if manifest.require_confirm and confirmed is not True:
            # 与进程内注册表相同的本地确认门。
            raise ConfirmationRequired(name)  # same local gate as the in-process registry
        merged = SkillRegistry._validate_params(manifest, {} if params is None else params)
        identity = operation or OperationIdentity.fresh()
        cmd = self._pb2.SkillCommand(
            command_id=uuid.uuid4().hex[:12],
            skill=name,
            params_json=json.dumps(merged, ensure_ascii=False, allow_nan=False),
            confirmed=confirmed,
            task_id=identity.task_id, operation_id=identity.operation_id,
            plan_revision=identity.plan_revision, api_version=identity.api_version,
        )
        terminal = {
            self._pb2.SkillEvent.SUCCEEDED, self._pb2.SkillEvent.FAILED, self._pb2.SkillEvent.HALTED,
        }
        last = None
        try:
            async for event in self._stub.InvokeSkill(cmd, timeout=max(manifest.termination.timeout_s + 10.0, 30.0)):
                last = event
                if event.phase in terminal:
                    break
        except grpc.aio.AioRpcError:
            return SkillResult(ok=False, detail="timeout or lost control response; query operation before retrying",
                               operation_id=identity.operation_id, invocation_state="OUTCOME_UNKNOWN",
                               stop_state="STOP_UNCONFIRMED",
                               data={"_execution": {"outcome": "OUTCOME_UNKNOWN", "stop_state": "STOP_UNCONFIRMED"}})
        if last is None or last.phase not in terminal:
            return SkillResult(ok=False, detail="timeout: no terminal response from control",
                               operation_id=identity.operation_id, invocation_state="OUTCOME_UNKNOWN",
                               stop_state="STOP_UNCONFIRMED")
        data: dict[str, Any] = {}
        if last.data_json:
            try:
                data = json.loads(last.data_json)
            except json.JSONDecodeError:
                data = {}
        ok = last.phase == self._pb2.SkillEvent.SUCCEEDED
        detail = last.detail or (last.error.message if last.HasField("error") else "")
        return SkillResult(ok=ok, detail=detail, data=data, operation_id=last.operation_id,
                           attempt_id=last.attempt_id, invocation_state=last.invocation_state,
                           stop_state=last.stop_state)

    async def get_operation(self, operation_id: str):
        return await self._stub.GetOperation(self._pb2.OperationRequest(operation_id=operation_id), timeout=5)

    @staticmethod
    def _task_result(reply):
        if reply.error:
            raise TaskJournalError(reply.error)
        return json.loads(reply.report_json) if reply.report_json else {"found": False}

    async def record_task_plan(self, payload):
        reply = await self._stub.RecordTaskPlan(self._pb2.TaskPlanRequest(plan_json=json.dumps(payload, allow_nan=False)), timeout=5)
        return self._task_result(reply)

    async def record_task_intent(self, task_id, revision, step_id, params):
        return await self._record_task_step(task_id, revision, step_id, "intent", params)

    async def record_task_result(self, task_id, revision, step_id, result):
        return await self._record_task_step(task_id, revision, step_id, "result", result)

    async def _record_task_step(self, task_id, revision, step_id, kind, payload):
        reply = await self._stub.RecordTaskStep(self._pb2.TaskStepRequest(task_id=task_id, revision=revision,
            step_id=step_id, kind=kind, payload_json=json.dumps(payload, allow_nan=False)), timeout=5)
        return self._task_result(reply)

    async def finish_task(self, task_id, *, interrupted=False):
        reply = await self._stub.FinishTask(self._pb2.TaskFinishRequest(task_id=task_id, interrupted=interrupted), timeout=5)
        return self._task_result(reply)

    async def get_task(self, task_id):
        return self._task_result(await self._stub.GetTask(self._pb2.TaskRequest(task_id=task_id), timeout=5))

    async def list_tasks(self, limit=20):
        reply = await self._stub.ListTasks(self._pb2.TaskListRequest(limit=limit), timeout=5)
        if reply.error:
            raise TaskJournalError(reply.error)
        return json.loads(reply.tasks_json)

    async def reconcile_task(self, task_id):
        from embodied.contracts.recovery import RecoveryReview
        from embodied.runtime.recovery import expire_effect

        started = time.monotonic()
        report = self._task_result(await self._stub.ReconcileTask(self._pb2.TaskRequest(task_id=task_id), timeout=5))
        elapsed = time.monotonic() - started
        try:
            review = RecoveryReview.model_validate(report["recovery_review"])
            if (review.task_id != task_id or report.get("task_id") != task_id
                    or report.get("automatic_resume_allowed") is not False or report.get("read_only") is not True
                    or report.get("found") and report.get("robot_id") != review.robot_id):
                raise ValueError("invalid recovery review identity or authority")
        except (KeyError, ValueError, TypeError) as exc:
            raise TaskJournalError("recovery_review_unavailable_or_invalid") from exc
        for step in report.get("steps", []):
            current = step["current_effect"]
            if not review.coherent:
                step["current_effect"] = {"eligible": False, "verdict": "unknown", "reason": "recovery_snapshot_changed"}
            else:
                expire_effect(current, elapsed, "task_report_transport_stale")
        report["assessment_transport_rtt_s"] = elapsed
        return report

    async def reconcile_effect(self, operation_id: str, attempt_id: str):
        return await self._stub.ReconcileEffect(self._pb2.EffectRequest(
            operation_id=operation_id, attempt_id=attempt_id,
        ), timeout=5)

    async def effect_verdict(self, operation_id: str, attempt_id: str, evidence_id: str, verification: dict) -> str:
        started = time.monotonic()
        try:
            reply = await self._stub.CheckEffect(self._pb2.EffectRequest(
                operation_id=operation_id, attempt_id=attempt_id, evidence_id=evidence_id,
                verification_digest=effect_digest(verification),
            ), timeout=EFFECT_MAX_AGE_S)
            age = reply.age_s + time.monotonic() - started
            if not reply.eligible or not 0 <= age < EFFECT_MAX_AGE_S:
                return "unknown"
            evidence = EffectEvidence.model_validate_json(reply.evidence_json)
            if (not evidence.intact() or evidence.operation_id != operation_id or evidence.attempt_id != attempt_id
                    or evidence.evidence_id != evidence_id or evidence.verification_digest != effect_digest(verification)
                    or reply.verdict != evidence.verdict):
                return "unknown"
            return evidence.verdict
        except (grpc.aio.AioRpcError, ValueError):
            return "unknown"

    async def cancel_operation(self, operation_id: str):
        return await self._stub.CancelOperation(self._pb2.OperationRequest(operation_id=operation_id), timeout=5)

    async def reconcile_stop(self, operation_id: str, attempt_id: str, *, confirmed: bool = False):
        if confirmed is not True:
            raise ConfirmationRequired("maintenance.reconcile_stop")
        return await self._stub.ReconcileStop(self._pb2.StopReconciliationRequest(
            operation_id=operation_id, attempt_id=attempt_id, confirmed=True,
        ), timeout=5)

    async def world_snapshot(self):
        started = time.monotonic()
        try:
            reply = await self._stub.GetWorldState(
                self._pb2.WorldStateRequest(), timeout=self._snapshot_admission.max_age_s,
            )
            if not reply.available:
                return None
            envelope = ObservationEnvelope.model_validate_json(reply.envelope_json)
            return self._snapshot_admission.accept(envelope, time.monotonic() - started, started)
        except (grpc.aio.AioRpcError, ValueError):
            return None

    async def world_context(self) -> str:
        snapshot = await self.world_snapshot()
        if snapshot is None:
            return "World state unavailable or stale; do not assume physical effects."
        objects = ", ".join(f"{name}@{pose.pos}" for name, pose in snapshot.objects.items())
        return f"simulated world: objects={objects}; regions={','.join(snapshot.regions)}"
