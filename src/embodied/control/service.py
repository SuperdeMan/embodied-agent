"""realtime-control process: gRPC ControlService over a SkillRegistry (+ optional sim).

Liveness contract (SG-2 chain, docs/architecture.md §4.5): the safety guardian holds
SupervisorLink open with periodic pings. When supervision is required, a stale/absent
link LATCHES the halt (only an explicit Reset releases it) — kill any upstream process
and the arm stops and stays stopped. The latch also drives the in-driver Guard when a
sim is attached, so an in-flight motion is denied at the write path, not just new calls.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import grpc

from embodied.runtime.rpcgen import import_common, import_control
from embodied.skills.registry import ConfirmationRequired, SkillRegistry

logger = logging.getLogger("control.service")

DEFAULT_PORT = 8391
SUPERVISOR_STALE_S = 1.5


class ControlState:
    def __init__(self, registry: SkillRegistry, sim: Any = None, *, require_supervisor: bool = False):
        self.registry = registry
        self.sim = sim
        self.require_supervisor = require_supervisor
        self.halted = False
        self.halt_reason = ""
        self.last_supervisor_ping = 0.0
        self.supervisor_connected = False

    def halt(self, reason: str) -> None:
        if not self.halted:
            logger.warning("HALT latched: %s", reason)
        self.halted = True
        self.halt_reason = reason
        if self.sim is not None and hasattr(self.sim, "guard"):
            self.sim.guard.estop(reason)  # deny in-flight writes at the driver path too

    def reset(self, reason: str) -> None:
        logger.info("reset (%s)", reason)
        self.halted = False
        self.halt_reason = ""
        if self.sim is not None and hasattr(self.sim, "guard"):
            self.sim.guard.reset()

    def supervision_ok(self) -> bool:
        if not self.require_supervisor:
            return True
        return self.supervisor_connected and (time.monotonic() - self.last_supervisor_ping) < SUPERVISOR_STALE_S


def build_servicer(state: ControlState):
    pb2, pb2_grpc = import_control()
    common = import_common()

    class Servicer(pb2_grpc.ControlServiceServicer):
        async def ListSkills(self, request, context):
            reply = pb2.ListSkillsReply()
            for m in state.registry.catalog():
                reply.skills.append(pb2.SkillInfo(manifest_json=m.model_dump_json()))
            return reply

        async def InvokeSkill(self, request, context):
            def ev(phase, detail="", error_code="", data=None):
                e = pb2.SkillEvent(command_id=request.command_id, phase=phase, detail=detail)
                if error_code:
                    e.error.code = error_code
                    e.error.message = detail
                if data:
                    e.data_json = json.dumps(data, ensure_ascii=False, default=str)
                return e

            if state.halted or not state.supervision_ok():
                reason = state.halt_reason or "supervisor_link_down"
                if not state.halted and state.require_supervisor:
                    state.halt(reason)  # absent supervision latches, not just rejects
                yield ev(pb2.SkillEvent.HALTED, f"halted: {reason}", "halted")
                return
            try:
                params = json.loads(request.params_json) if request.params_json else {}
            except json.JSONDecodeError:
                yield ev(pb2.SkillEvent.FAILED, "bad params_json", "bad_request")
                return
            yield ev(pb2.SkillEvent.ACCEPTED)
            try:
                result = await state.registry.invoke(
                    request.skill, params, confirmed=bool(request.confirmed)
                )
            except ConfirmationRequired:
                yield ev(pb2.SkillEvent.FAILED, "confirmation required", "need_confirm")
                return
            except Exception as e:
                yield ev(pb2.SkillEvent.FAILED, f"{type(e).__name__}: {e}", "invoke_error")
                return
            if state.halted:  # halt raced the motion: report honestly
                yield ev(pb2.SkillEvent.HALTED, f"halted during execution: {state.halt_reason}", "halted")
                return
            phase = pb2.SkillEvent.SUCCEEDED if result.ok else pb2.SkillEvent.FAILED
            yield ev(phase, result.detail, "" if result.ok else "skill_failed", data=result.data)

        async def StreamState(self, request, context):
            interval = max(0.02, float(request.min_interval_ms or 100) / 1000.0)
            while not context.cancelled():
                if state.sim is not None:
                    obs = state.sim.read()
                    msg = common.RobotState(
                        embodiment_id=state.sim.spec().embodiment_id,
                        gripper_opening=obs.gripper_opening,
                    )
                    msg.stamp.unix_ms = int(time.time() * 1000)
                    msg.joints.position.extend(obs.qpos)
                    msg.joints.velocity.extend(obs.qvel)
                    p = obs.ee_pose
                    msg.ee_pose.x, msg.ee_pose.y, msg.ee_pose.z = p.pos
                    msg.ee_pose.qw, msg.ee_pose.qx, msg.ee_pose.qy, msg.ee_pose.qz = p.quat
                    yield msg
                await asyncio.sleep(interval)

        async def Halt(self, request, context):
            state.halt(request.reason or "halt_requested")
            return pb2.HaltReply(halted=True)

        async def Reset(self, request, context):
            state.reset(request.reason or "reset_requested")
            return pb2.ResetReply(reset=True)

        async def SupervisorLink(self, request_iterator, context):
            state.supervisor_connected = True
            state.last_supervisor_ping = time.monotonic()
            logger.info("supervisor link up")
            try:
                async for ping in request_iterator:
                    state.last_supervisor_ping = time.monotonic()
                    pong = common.Timestamp(unix_ms=int(time.time() * 1000))
                    yield pong
            finally:
                state.supervisor_connected = False
                if state.require_supervisor:
                    state.halt("supervisor_link_lost")
                logger.warning("supervisor link down")

    return Servicer()


async def watchdog_loop(state: ControlState, interval_s: float = 0.2) -> None:
    """Latches halt when a required supervisor goes stale WITHOUT the stream closing
    (e.g. frozen process: TCP stays up, pings stop)."""
    while True:
        if (
            state.require_supervisor and state.supervisor_connected
            and (time.monotonic() - state.last_supervisor_ping) >= SUPERVISOR_STALE_S
            and not state.halted
        ):
            state.halt("supervisor_stale")
        await asyncio.sleep(interval_s)


async def serve(state: ControlState, port: int = DEFAULT_PORT, host: str = "127.0.0.1"):
    _, pb2_grpc = import_control()
    server = grpc.aio.server()
    pb2_grpc.add_ControlServiceServicer_to_server(build_servicer(state), server)
    bound = server.add_insecure_port(f"{host}:{port}")
    await server.start()
    logger.info("control service on %s:%d (require_supervisor=%s)", host, bound, state.require_supervisor)
    return server, bound
