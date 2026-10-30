"""RemoteSkillRegistry: agent-core's view of skills served by the realtime-control process.

Duck-types SkillRegistry for the planner engine (catalog/tool_schemas/resolve_tool/get/
invoke); manifests come from ListSkills, invocation streams SkillEvents until terminal.
The confirm UX stays in agent-core; the `confirmed` flag rides the SkillCommand — the
control process still re-checks require_confirm at its own registry (defense in depth).


远端注册表从控制服务获取 manifest 并流式调用至终态；Agent 负责确认交互，确认标记跨 RPC 传递，控制端再独立校验。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import grpc

from embodied.contracts.operation import OperationIdentity
from embodied.runtime.rpcgen import import_control
from embodied.skills.manifest import SkillManifest
from embodied.skills.registry import ConfirmationRequired, SkillNotFound, SkillRegistry, SkillResult


class RemoteSkillRegistry:
    def __init__(self, channel: grpc.aio.Channel):
        _, pb2_grpc = import_control()
        self._pb2, _ = import_control()
        self._stub = pb2_grpc.ControlServiceStub(channel)
        self._manifests: dict[str, SkillManifest] = {}

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

    async def cancel_operation(self, operation_id: str):
        return await self._stub.CancelOperation(self._pb2.OperationRequest(operation_id=operation_id), timeout=5)
