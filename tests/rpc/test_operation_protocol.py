"""Durable identities over real loopback RPC. 真实本机 RPC 上的持久操作身份验证。"""

import asyncio
from contextlib import asynccontextmanager

import pytest

grpc = pytest.importorskip("grpc")

from embodied.runtime.rpcgen import GEN_DIR  # noqa: E402

if not (GEN_DIR / "embodiedrpc").is_dir():
    pytest.skip("proto stubs not generated", allow_module_level=True)

from embodied.contracts.operation import OperationIdentity  # noqa: E402
from embodied.control.service import ControlState, serve  # noqa: E402
from embodied.runtime.operations import OperationLedger  # noqa: E402
from embodied.runtime.rpcgen import import_control  # noqa: E402
from embodied.skills.manifest import SkillManifest  # noqa: E402
from embodied.skills.registry import SkillRegistry, SkillResult  # noqa: E402
from embodied.skills.remote import RemoteSkillRegistry  # noqa: E402


@asynccontextmanager
async def runtime(path, handler):
    registry = SkillRegistry(ledger=OperationLedger(path))
    registry.register(SkillManifest(name="skill.test.act", description="action", resources=["arm"]), handler)
    state = ControlState(registry)
    server, port = await serve(state, port=0)
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    remote = await RemoteSkillRegistry(channel).connect()
    try:
        yield registry, remote, channel, state
    finally:
        await channel.close()
        registry.cancel_all()
        await server.stop(None)
        registry.close()


async def test_completed_operation_survives_server_restart_and_reconnect(tmp_path):
    calls = []

    async def action():
        calls.append(1)
        return SkillResult(ok=True, data={"result": 42})

    identity = OperationIdentity(task_id="task", operation_id="operation")
    path = tmp_path / "rpc.sqlite3"
    async with runtime(path, action) as (_, remote, _, _):
        first = await remote.invoke("skill.test.act", operation=identity)
        query = await remote.get_operation(identity.operation_id)
        assert first.ok and query.found and query.state == "RETURNED"
        assert query.attempt_id == first.attempt_id
        assert query.stop_state == "NOT_REQUESTED"
    async with runtime(path, action) as (_, remote, _, _):
        replay = await remote.invoke("skill.test.act", operation=identity)
        assert replay.ok and replay.data == {"result": 42}
        assert replay.attempt_id == first.attempt_id and calls == [1]


async def test_cancel_reply_is_unknown_and_reset_cannot_clear_quarantine(tmp_path):
    started = asyncio.Event()

    async def action():
        started.set()
        await asyncio.Event().wait()

    identity = OperationIdentity(task_id="task", operation_id="operation")
    async with runtime(tmp_path / "rpc.sqlite3", action) as (_, remote, channel, _):
        call = asyncio.create_task(remote.invoke("skill.test.act", operation=identity))
        await asyncio.wait_for(started.wait(), 2)
        cancellation = await remote.cancel_operation(identity.operation_id)
        assert cancellation.state == "OUTCOME_UNKNOWN"
        assert cancellation.stop_state == "STOP_UNCONFIRMED"
        result = await asyncio.wait_for(call, 2)
        assert not result.ok
        pb2, pb2_grpc = import_control()
        await pb2_grpc.ControlServiceStub(channel).Reset(pb2.ResetRequest(reason="test"))
        blocked = await remote.invoke("skill.test.act")
        assert not blocked.ok and "resource_unavailable" in blocked.detail


async def test_rpc_without_identity_or_with_unknown_version_never_dispatches(tmp_path):
    calls = []

    async def action():
        calls.append(1)
        return SkillResult(ok=True)

    async with runtime(tmp_path / "rpc.sqlite3", action) as (_, remote, channel, _):
        pb2, pb2_grpc = import_control()
        stub = pb2_grpc.ControlServiceStub(channel)
        for command in [
            pb2.SkillCommand(skill="skill.test.act"),
            pb2.SkillCommand(skill="skill.test.act", command_id="op", api_version="unknown/v99"),
        ]:
            events = [event async for event in stub.InvokeSkill(command)]
            assert events[-1].phase == pb2.SkillEvent.FAILED
        assert not calls
        assert not (await remote.get_operation("absent")).found


async def test_rpc_halt_revokes_running_operation(tmp_path):
    started = asyncio.Event()

    async def action():
        started.set()
        await asyncio.Event().wait()

    identity = OperationIdentity(task_id="task", operation_id="operation")
    async with runtime(tmp_path / "rpc.sqlite3", action) as (_, remote, channel, _):
        call = asyncio.create_task(remote.invoke("skill.test.act", operation=identity))
        await asyncio.wait_for(started.wait(), 2)
        pb2, pb2_grpc = import_control()
        await pb2_grpc.ControlServiceStub(channel).Halt(pb2.HaltRequest(reason="test"))
        assert not (await asyncio.wait_for(call, 2)).ok
        record = await remote.get_operation(identity.operation_id)
        assert record.state == "OUTCOME_UNKNOWN" and record.stop_state == "STOP_UNCONFIRMED"
