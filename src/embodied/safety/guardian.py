"""safety-guardian process (SG-2): deterministic supervisor, trusts no AI output.

Liveness chain: agent-core streams Heartbeat here; the guardian relays its own pings to
realtime-control over SupervisorLink. Consequences (all fail-closed):
- agent heartbeats stop/stream drops  -> guardian issues control.Halt("agent_lost")
- guardian dies                       -> control's SupervisorLink drops -> control latches
- control unreachable                 -> nothing to protect through this path; guardian
                                          keeps retrying so recovery is automatic
ValidateCommand offers pre-motion geometric checks against the static fence config —
independent of the in-driver Guard (defense in depth, not a replacement).
"""

from __future__ import annotations

import asyncio
import logging
import time

import grpc

from embodied.runtime.rpcgen import import_common, import_control, import_safety
from embodied.safety.guard import GuardLimits

logger = logging.getLogger("safety.guardian")

DEFAULT_PORT = 8392
AGENT_STALE_S = 2.0
SUPERVISOR_PING_INTERVAL_S = 0.3


class GuardianState:
    def __init__(self, control_addr: str, limits: GuardLimits | None = None):
        self.control_addr = control_addr
        self.limits = limits or GuardLimits(joint_lower=(), joint_upper=())
        self.last_agent_beat = 0.0
        self.agent_connected = False
        self.ever_connected = False
        self.halt_sent = False

    def should_halt(self) -> tuple[bool, str]:
        """Single decision point, polled by the watchdog loop. Enforcement lives THERE and
        not in stream teardown: gRPC cancels the Heartbeat servicer coroutine when the
        client drops, so an `await send_halt()` in its finally block dies mid-flight —
        the halt would be logged but never delivered (caught by the split contract test)."""
        if self.halt_sent:
            return False, ""
        if self.agent_connected and (time.monotonic() - self.last_agent_beat) >= AGENT_STALE_S:
            return True, "agent_stale"
        if self.ever_connected and not self.agent_connected:
            return True, "agent_link_lost"
        return False, ""


async def _control_stub(state: GuardianState):
    _, pb2_grpc = import_control()
    channel = grpc.aio.insecure_channel(state.control_addr)
    return channel, pb2_grpc.ControlServiceStub(channel)


async def send_halt(state: GuardianState, reason: str) -> bool:
    pb2, _ = import_control()
    try:
        channel, stub = await _control_stub(state)
        try:
            await stub.Halt(pb2.HaltRequest(reason=reason), timeout=2.0)
            state.halt_sent = True
            logger.warning("halt sent to control: %s", reason)
            return True
        finally:
            await channel.close()
    except Exception as e:
        logger.error("halt delivery failed (%s): %s", reason, e)
        return False


def build_servicer(state: GuardianState):
    s_pb2, s_pb2_grpc = import_safety()

    class Servicer(s_pb2_grpc.SafetyServiceServicer):
        async def ValidateCommand(self, request, context):
            cur = request.current
            pos = (cur.ee_pose.x, cur.ee_pose.y, cur.ee_pose.z)
            fmin, fmax = state.limits.ee_fence_min, state.limits.ee_fence_max
            inside = all(fmin[i] <= pos[i] <= fmax[i] for i in range(3))
            if inside:
                return s_pb2.ValidateReply(allowed=True)
            return s_pb2.ValidateReply(allowed=False, reason=f"ee_outside_fence:{pos}")

        async def Heartbeat(self, request_iterator, context):
            common = import_common()
            state.agent_connected = True
            state.ever_connected = True
            state.last_agent_beat = time.monotonic()
            state.halt_sent = False  # a live agent starts a fresh accountability window
            logger.info("agent heartbeat up")
            try:
                async for ping in request_iterator:
                    state.last_agent_beat = time.monotonic()
                    yield s_pb2.HeartbeatPong(stamp=common.Timestamp(unix_ms=int(time.time() * 1000)))
            finally:
                # Flags only — no awaits here (this coroutine is being cancelled);
                # the watchdog loop delivers the halt.
                state.agent_connected = False
                logger.warning("agent heartbeat down")

        async def EStop(self, request, context):
            ok = await send_halt(state, request.reason or "estop")
            return s_pb2.EStopReply(stopped=ok)

    return Servicer()


async def agent_watchdog_loop(state: GuardianState, interval_s: float = 0.2) -> None:
    """Sole halt enforcement point: frozen agent (stream up, pings stopped) AND dropped
    agent (stream down). Failed delivery retries next tick — control may blip."""
    while True:
        need, reason = state.should_halt()
        if need:
            await send_halt(state, reason)
        await asyncio.sleep(interval_s)


async def supervisor_link_loop(state: GuardianState) -> None:
    """Hold SupervisorLink open to control; reconnect forever (control halts while we're gone)."""
    pb2, pb2_grpc = import_control()
    common = import_common()
    while True:
        try:
            channel = grpc.aio.insecure_channel(state.control_addr)
            stub = pb2_grpc.ControlServiceStub(channel)

            async def pings():
                while True:
                    yield common.Timestamp(unix_ms=int(time.time() * 1000))
                    await asyncio.sleep(SUPERVISOR_PING_INTERVAL_S)

            logger.info("supervisor link connecting to %s", state.control_addr)
            async for _pong in stub.SupervisorLink(pings()):
                pass
        except Exception as e:
            logger.warning("supervisor link dropped: %s", e)
        finally:
            try:
                await channel.close()
            except Exception:
                pass
        await asyncio.sleep(0.5)


async def serve(state: GuardianState, port: int = DEFAULT_PORT, host: str = "127.0.0.1"):
    _, s_pb2_grpc = import_safety()
    server = grpc.aio.server()
    s_pb2_grpc.add_SafetyServiceServicer_to_server(build_servicer(state), server)
    bound = server.add_insecure_port(f"{host}:{port}")
    await server.start()
    logger.info("guardian on %s:%d, watching control at %s", host, bound, state.control_addr)
    return server, bound
