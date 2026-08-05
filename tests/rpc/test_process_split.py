"""Three-process topology contract tests: control/guardian gRPC + liveness chain.

Auto-skip when grpcio or generated stubs are absent (CI installs no rpc group; run
locally via `uv run --group rpc pytest tests/rpc`). Mock skills only — no mujoco.
"""

from __future__ import annotations

import asyncio

import pytest

grpc = pytest.importorskip("grpc")

from embodied.runtime.rpcgen import GEN_DIR  # noqa: E402

if not (GEN_DIR / "embodiedrpc").is_dir():
    pytest.skip("proto stubs not generated (scripts/gen-proto)", allow_module_level=True)

from embodied.control import service as control_service  # noqa: E402
from embodied.control.service import ControlState, build_servicer, watchdog_loop  # noqa: E402
from embodied.runtime import liveness  # noqa: E402
from embodied.runtime.rpcgen import import_common, import_control, import_safety  # noqa: E402
from embodied.safety import guardian as guardian_mod  # noqa: E402
from embodied.safety.guard import GuardLimits  # noqa: E402
from embodied.skills.manifest import ParamSpec, SkillManifest  # noqa: E402
from embodied.skills.registry import ConfirmationRequired, SkillRegistry, SkillResult  # noqa: E402
from embodied.skills.remote import RemoteSkillRegistry  # noqa: E402


def make_registry() -> SkillRegistry:
    r = SkillRegistry()

    async def echo(**kw) -> SkillResult:
        return SkillResult(ok=True, detail="echoed", data={"echo": kw})

    async def danger(**kw) -> SkillResult:
        return SkillResult(ok=True, detail="armed")

    async def fail(**kw) -> SkillResult:
        return SkillResult(ok=False, detail="deliberate failure")

    r.register(
        SkillManifest(name="skill.test.echo", description="echo",
                      params={"x": ParamSpec(type="integer", required=False)}),
        echo,
    )
    r.register(SkillManifest(name="skill.test.danger", description="d", require_confirm=True), danger)
    r.register(SkillManifest(name="skill.test.fail", description="f"), fail)
    return r


async def start_control(**kw):
    _, pb2_grpc = import_control()
    state = ControlState(make_registry(), None, **kw)
    server = grpc.aio.server()
    pb2_grpc.add_ControlServiceServicer_to_server(build_servicer(state), server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    return state, server, port


async def start_guardian(control_port: int):
    _, s_pb2_grpc = import_safety()
    state = guardian_mod.GuardianState(
        f"127.0.0.1:{control_port}",
        GuardLimits(joint_lower=(), joint_upper=()),
    )
    server = grpc.aio.server()
    s_pb2_grpc.add_SafetyServiceServicer_to_server(guardian_mod.build_servicer(state), server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    return state, server, port


@pytest.fixture
async def control():
    state, server, port = await start_control()
    yield state, port
    await server.stop(None)


async def remote(port: int) -> tuple[RemoteSkillRegistry, grpc.aio.Channel]:
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    reg = await RemoteSkillRegistry(channel).connect()
    return reg, channel


async def test_list_skills_roundtrip(control):
    _, port = control
    reg, channel = await remote(port)
    try:
        names = [m.name for m in reg.catalog()]
        assert names == ["skill.test.danger", "skill.test.echo", "skill.test.fail"]
        assert reg.get("skill.test.danger").require_confirm is True
        assert reg.resolve_tool("skill-test-echo") == "skill.test.echo"
    finally:
        await channel.close()


async def test_invoke_success_failure_and_data(control):
    _, port = control
    reg, channel = await remote(port)
    try:
        r = await reg.invoke("skill.test.echo", {"x": 7})
        assert r.ok and r.detail == "echoed" and r.data == {"echo": {"x": 7}}
        r2 = await reg.invoke("skill.test.fail")
        assert not r2.ok and "deliberate" in r2.detail
    finally:
        await channel.close()


async def test_confirm_gate_holds_across_the_wire(control):
    _, port = control
    reg, channel = await remote(port)
    try:
        with pytest.raises(ConfirmationRequired):
            await reg.invoke("skill.test.danger")
        r = await reg.invoke("skill.test.danger", confirmed=True)
        assert r.ok
    finally:
        await channel.close()


async def test_halt_latches_until_reset(control):
    state, port = control
    pb2, pb2_grpc = import_control()
    reg, channel = await remote(port)
    stub = pb2_grpc.ControlServiceStub(channel)
    try:
        await stub.Halt(pb2.HaltRequest(reason="test_estop"))
        assert state.halted
        r = await reg.invoke("skill.test.echo")
        assert not r.ok and "halted" in r.detail  # latched: rejected, not queued
        await stub.Reset(pb2.ResetRequest(reason="operator"))
        assert not state.halted
        r2 = await reg.invoke("skill.test.echo")
        assert r2.ok
    finally:
        await channel.close()


async def test_require_supervisor_fails_closed():
    state, server, port = await start_control(require_supervisor=True)
    reg, channel = await remote(port)
    try:
        r = await reg.invoke("skill.test.echo")
        assert not r.ok and "halted" in r.detail
        assert state.halted and "supervisor" in state.halt_reason
    finally:
        await channel.close()
        await server.stop(None)


async def test_supervisor_link_enables_and_its_loss_latches(monkeypatch):
    monkeypatch.setattr(control_service, "SUPERVISOR_STALE_S", 0.4)
    state, server, port = await start_control(require_supervisor=True)
    gstate = guardian_mod.GuardianState(f"127.0.0.1:{port}", GuardLimits(joint_lower=(), joint_upper=()))
    link = asyncio.create_task(guardian_mod.supervisor_link_loop(gstate))
    reg, channel = await remote(port)
    try:
        for _ in range(40):  # wait for the link to come up
            if state.supervisor_connected:
                break
            await asyncio.sleep(0.05)
        assert state.supervisor_connected
        r = await reg.invoke("skill.test.echo")
        assert r.ok

        link.cancel()  # guardian dies -> link drops -> control latches
        for _ in range(40):
            if state.halted:
                break
            await asyncio.sleep(0.05)
        assert state.halted and "supervisor" in state.halt_reason
        r2 = await reg.invoke("skill.test.echo")
        assert not r2.ok
    finally:
        link.cancel()
        await channel.close()
        await server.stop(None)


async def test_agent_heartbeat_loss_triggers_guardian_halt(monkeypatch):
    monkeypatch.setattr(guardian_mod, "AGENT_STALE_S", 0.5)
    monkeypatch.setattr(liveness, "PING_INTERVAL_S", 0.1)
    cstate, cserver, cport = await start_control()
    gstate, gserver, gport = await start_guardian(cport)
    wd = asyncio.create_task(guardian_mod.agent_watchdog_loop(gstate, interval_s=0.1))
    beat = asyncio.create_task(liveness.agent_heartbeat_loop(f"127.0.0.1:{gport}"))
    try:
        for _ in range(40):
            if gstate.agent_connected:
                break
            await asyncio.sleep(0.05)
        assert gstate.agent_connected
        assert not cstate.halted

        beat.cancel()  # agent-core dies
        for _ in range(60):
            if cstate.halted:
                break
            await asyncio.sleep(0.05)
        assert cstate.halted and "agent" in cstate.halt_reason
    finally:
        beat.cancel()
        wd.cancel()
        await gserver.stop(None)
        await cserver.stop(None)


async def test_validate_command_fence():
    s_pb2, s_pb2_grpc = import_safety()
    common = import_common()
    cstate, cserver, cport = await start_control()
    gstate, gserver, gport = await start_guardian(cport)
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{gport}")
    stub = s_pb2_grpc.SafetyServiceStub(channel)
    try:
        inside = common.RobotState()
        inside.ee_pose.x, inside.ee_pose.y, inside.ee_pose.z = 0.1, 0.1, 0.2
        r = await stub.ValidateCommand(s_pb2.ValidateRequest(skill="skill.test.echo", current=inside))
        assert r.allowed
        outside = common.RobotState()
        outside.ee_pose.x, outside.ee_pose.y, outside.ee_pose.z = 2.0, 0.0, 0.2
        r2 = await stub.ValidateCommand(s_pb2.ValidateRequest(skill="skill.test.echo", current=outside))
        assert not r2.allowed and "fence" in r2.reason
    finally:
        await channel.close()
        await gserver.stop(None)
        await cserver.stop(None)


async def test_stale_supervisor_watchdog(monkeypatch):
    """Frozen guardian: stream stays up, pings stop -> watchdog latches."""
    monkeypatch.setattr(control_service, "SUPERVISOR_STALE_S", 0.3)
    state, server, port = await start_control(require_supervisor=True)
    state.supervisor_connected = True  # simulate an open-but-frozen link
    state.last_supervisor_ping = __import__("time").monotonic()
    wd = asyncio.create_task(watchdog_loop(state, interval_s=0.05))
    try:
        await asyncio.sleep(0.6)
        assert state.halted and state.halt_reason == "supervisor_stale"
    finally:
        wd.cancel()
        await server.stop(None)
