"""RemoteSkillRegistry: agent-core's view of skills served by the realtime-control process.

Duck-types SkillRegistry for the planner engine (catalog/tool_schemas/resolve_tool/get/
invoke); manifests come from ListSkills, invocation streams SkillEvents until terminal.
The confirm UX stays in agent-core; the `confirmed` flag rides the SkillCommand — the
control process still re-checks require_confirm at its own registry (defense in depth).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import grpc

from embodied.runtime.rpcgen import import_control
from embodied.skills.manifest import SkillManifest
from embodied.skills.registry import ConfirmationRequired, SkillNotFound, SkillResult


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

    async def invoke(self, name: str, params: dict[str, Any] | None = None, *, confirmed: bool = False) -> SkillResult:
        manifest = self.get(name)
        if manifest.require_confirm and not confirmed:
            raise ConfirmationRequired(name)  # same local gate as the in-process registry
        cmd = self._pb2.SkillCommand(
            command_id=uuid.uuid4().hex[:12],
            skill=name,
            params_json=json.dumps(params or {}, ensure_ascii=False, default=str),
            confirmed=confirmed,
        )
        terminal = {
            self._pb2.SkillEvent.SUCCEEDED, self._pb2.SkillEvent.FAILED, self._pb2.SkillEvent.HALTED,
        }
        last = None
        async for event in self._stub.InvokeSkill(cmd, timeout=max(manifest.termination.timeout_s + 10.0, 30.0)):
            last = event
            if event.phase in terminal:
                break
        if last is None:
            return SkillResult(ok=False, detail="no response from control")
        data: dict[str, Any] = {}
        if last.data_json:
            try:
                data = json.loads(last.data_json)
            except json.JSONDecodeError:
                data = {}
        ok = last.phase == self._pb2.SkillEvent.SUCCEEDED
        detail = last.detail or (last.error.message if last.HasField("error") else "")
        return SkillResult(ok=ok, detail=detail, data=data)
