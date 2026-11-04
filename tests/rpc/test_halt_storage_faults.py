"""Supervision remains latched through database faults. 数据库故障不打断监督闩锁。"""

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

grpc = pytest.importorskip("grpc")

from embodied.runtime.rpcgen import GEN_DIR  # noqa: E402

if not (GEN_DIR / "embodiedrpc").is_dir():
    pytest.skip("proto stubs not generated", allow_module_level=True)

from embodied.contracts.operation import OperationIdentity  # noqa: E402
from embodied.control.service import ControlState, serve, watchdog_loop  # noqa: E402
from embodied.runtime.invocation import CURRENT_INVOCATION  # noqa: E402
from embodied.runtime.operations import OperationLedger  # noqa: E402
from embodied.runtime.rpcgen import import_control  # noqa: E402
from embodied.safety.guard import Guard, GuardLimits  # noqa: E402
from embodied.skills.manifest import SkillManifest  # noqa: E402
from embodied.skills.registry import SkillRegistry  # noqa: E402


@pytest.mark.parametrize("trigger", ["rpc_halt", "stale_supervisor", "guard_error"])
async def test_halt_revokes_every_permit_before_failed_persistence(tmp_path, monkeypatch, trigger):
    registry = SkillRegistry(ledger=OperationLedger(tmp_path / "ops.sqlite3"))
    guard = Guard(GuardLimits(joint_lower=(), joint_upper=()))
    state = ControlState(registry, SimpleNamespace(guard=guard), require_supervisor=True)
    permits = []

    async def wait():
        permits.append(CURRENT_INVOCATION.get())
        await asyncio.Event().wait()

    calls = []
    for i in range(2):
        name = f"skill.test.wait{i}"
        registry.register(SkillManifest(name=name, description="wait", resources=[f"arm{i}"]), wait)
        calls.append(asyncio.create_task(registry.invoke(name, operation=OperationIdentity(task_id="task", operation_id=f"op{i}"))))
    async with asyncio.timeout(2):
        while len(permits) < 2:
            await asyncio.sleep(0)
    server, port = await serve(state, port=0)
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    watchdog = None
    writes = []

    def failed_write(*args, **kwargs):
        assert state.halted
        assert guard.halted or trigger == "guard_error"
        for permit in permits:
            with permit.access() as allowed:
                assert not allowed
        assert all(task.cancelling() for task in calls)
        writes.append(args[0])
        raise sqlite3.OperationalError("injected write failure")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(registry.ledger, "finish", failed_write)
            if trigger == "rpc_halt":
                pb2, pb2_grpc = import_control()
                reply = await pb2_grpc.ControlServiceStub(channel).Halt(pb2.HaltRequest(reason="test"))
                assert reply.halted
            elif trigger == "stale_supervisor":
                state.supervisor_connected = True
                watchdog = asyncio.create_task(watchdog_loop(state, interval_s=0.005))
                async with asyncio.timeout(2):
                    while not state.halted:
                        await asyncio.sleep(0)
                await asyncio.sleep(0.02)
                assert not watchdog.done()
            else:
                def broken_guard(reason):
                    raise RuntimeError("guard unavailable")
                patch.setattr(guard, "estop", broken_guard)
                with pytest.raises(RuntimeError, match="guard unavailable"):
                    state.halt("test")
            assert state.halt_persistence_errors == {"op0": "OperationalError", "op1": "OperationalError"}
            await asyncio.gather(*calls, return_exceptions=True)
        assert set(writes) == {"op0", "op1"} and not registry._active
        assert all(registry.ledger.get(f"op{i}").state == "RUNNING" for i in range(2))
        assert not registry.arbiter.idle(("arm0", "arm1"))
    finally:
        if watchdog is not None:
            watchdog.cancel()
            await asyncio.gather(watchdog, return_exceptions=True)
        await channel.close()
        await server.stop(None)
        registry.close()
