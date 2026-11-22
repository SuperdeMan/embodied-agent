"""realtime-control process: gRPC ControlService over a SkillRegistry (+ optional sim).

Liveness contract (SG-2 chain, docs/architecture.md §4.5): the safety guardian holds
SupervisorLink open with periodic pings. When supervision is required, a stale/absent
link LATCHES the halt (only an explicit Reset releases it) — kill any upstream process
and the arm stops and stays stopped. The latch also drives the in-driver Guard when a
sim is attached, so an in-flight motion is denied at the write path, not just new calls.

When configured with mTLS and RpcAccessPolicy (D033), every RPC verifies the BoringSSL
client certificate fingerprint and enforces the hardcoded agent/guardian/operator role
matrix before touching state or the ledger. Non-loopback binds refuse plaintext.


控制进程通过 gRPC 提供技能。监督链缺失或过期时闩锁，显式重置才释放；有仿真时同时闩锁驱动 Guard，阻断进行中的写入。配置 mTLS 与策略后（D033），所有接口先验客户端证书指纹与角色矩阵，非回环地址拒绝明文监听。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from dataclasses import asdict
from typing import Any, Callable

import grpc

from embodied.contracts.effect import EffectRejected
from embodied.contracts.observation import ObservationEnvelope
from embodied.contracts.operation import OperationIdentity
from embodied.contracts.rpc_auth import RpcAccessPolicy, RpcTlsPaths
from embodied.contracts.stopping import StopRejected
from embodied.runtime.observation import ObservationUnavailable
from embodied.runtime.rpc_auth import (
    RpcAuthorizer,
    RpcForbidden,
    RpcPrincipal,
    RpcUnauthenticated,
    is_loopback_host,
    load_server_credentials,
    record_rpc_audit,
)
from embodied.runtime.rpcgen import import_common, import_control
from embodied.skills.registry import ConfirmationRequired, SkillRegistry

logger = logging.getLogger("control.service")

DEFAULT_PORT = 8391
SUPERVISOR_STALE_S = 1.5


class ControlState:
    def __init__(self, registry: SkillRegistry, sim: Any = None, *, require_supervisor: bool = False,
                 world_reader: Callable | None = None, authorizer: RpcAuthorizer | None = None):
        self.registry = registry
        self.sim = sim
        self.require_supervisor = require_supervisor
        self.halted = False
        self.halt_reason = ""
        self.halt_persistence_errors: dict[str, str] = {}
        self.last_supervisor_ping = 0.0
        self.supervisor_connected = False
        self.world_reader = world_reader
        self.world_sequence = 0
        self.authorizer = authorizer
        self.last_reset_audit: dict[str, Any] | None = None

    def halt(self, reason: str) -> None:
        if not self.halted:
            logger.warning("HALT latched: %s", reason)
        self.halted = True
        self.halt_reason = reason
        # 驱动闩锁先于数据库；即使 Guard 抛错，也尝试撤销所有调用许可。
        # Latch the driver before database work; revoke all permits even if Guard raises.
        try:
            if self.sim is not None and hasattr(self.sim, "guard"):
                self.sim.guard.estop(reason)
        finally:
            self.halt_persistence_errors.update(self.registry.cancel_all())

    def reset(self, reason: str, *, principal: RpcPrincipal | None = None) -> None:
        if principal is not None:
            self.last_reset_audit = {
                "api_version": "embodied.dev/v1alpha1", "issuer": principal.issuer,
                "subject_hash": principal.subject_hash, "cert_sha256": principal.cert_sha256,
                "role": principal.role, "policy_digest": principal.policy_digest,
                "scope": "reset_guard", "reason": reason, "recorded_unix": time.time(),
            }
            logger.warning("rpc_guard_reset subject=%s cert=%s policy=%s reason=%s",
                           principal.subject_hash, principal.cert_sha256, principal.policy_digest, reason)
        else:
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

    async def _auth(context, method: str) -> RpcPrincipal | None:
        if state.authorizer is None:
            return None
        try:
            return state.authorizer.authorize(context.auth_context(), method)
        except RpcUnauthenticated as exc:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, str(exc))
        except RpcForbidden as exc:
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, str(exc))

    def _audit_body(principal: RpcPrincipal, scope: str, method: str, record, **extra) -> dict[str, Any]:
        return {
            "api_version": "embodied.dev/v1alpha1", "issuer": principal.issuer,
            "subject_hash": principal.subject_hash, "cert_sha256": principal.cert_sha256,
            "role": principal.role, "policy_digest": principal.policy_digest,
            "scope": scope, "method": method, "task_id": record.task_id,
            "request_digest": record.request_digest, "ledger_boot_id": state.registry.ledger.boot_id,
            "recorded_unix": time.time(), **extra,
        }

    def _guard_halted() -> bool:
        if state.sim is not None and hasattr(state.sim, "guard"):
            return bool(state.sim.guard.halted)
        return bool(state.halted)

    class Servicer(pb2_grpc.ControlServiceServicer):
        async def GetWorldState(self, request, context):
            await _auth(context, "GetWorldState")
            if state.world_reader is None:
                return pb2.WorldStateReply(available=False, reason="world_state_unavailable")
            started = time.monotonic()
            try:
                epoch, obs = await state.registry.observe(state.world_reader)
                state.world_sequence += 1
                envelope = ObservationEnvelope(
                    robot_id=state.registry.robot_id, boot_id=state.registry.ledger.boot_id,
                    epoch=epoch, sequence=state.world_sequence, capture_timestamp=obs.t,
                    capture_duration_s=time.monotonic() - started, calibration_version="mujoco_reference_v1",
                    qpos=list(obs.qpos), qvel=list(obs.qvel),
                    ee_pose={"pos": obs.ee_pose.pos, "quat": obs.ee_pose.quat},
                    gripper_opening=obs.gripper_opening,
                    objects={name: {"pos": pose.pos, "quat": pose.quat} for name, pose in obs.objects.items()},
                    regions=obs.extras.get("regions", {}),
                )
                return pb2.WorldStateReply(available=True, envelope_json=envelope.model_dump_json())
            except Exception:
                return pb2.WorldStateReply(available=False, reason="invalid_world_state")

        def _operation_reply(self, record):
            if record is None:
                return pb2.OperationReply(found=False)
            return pb2.OperationReply(
                found=True, operation_id=record.operation_id, attempt_id=record.attempt_id,
                state=record.state, stop_state=record.stop_state, boot_id=record.boot_id,
                lease_epoch=record.lease_epoch,
                result_json=json.dumps(asdict(state.registry._from_record(record)), ensure_ascii=False, allow_nan=False),
                stop_evidence_json=json.dumps(record.stop_evidence, ensure_ascii=False, allow_nan=False)
                if record.stop_evidence else "",
                effect_evidence_json=json.dumps(record.effect_evidence, ensure_ascii=False, allow_nan=False)
                if record.effect_evidence else "",
            )

        async def GetOperation(self, request, context):
            await _auth(context, "GetOperation")
            return self._operation_reply(state.registry.get_operation(request.operation_id))

        def _task_reply(self, report=None, error=""):
            return pb2.TaskReply(found=bool(report and report.get("found")),
                                 report_json=json.dumps(report, ensure_ascii=False, allow_nan=False) if report else "",
                                 error=error)

        async def RecordTaskPlan(self, request, context):
            await _auth(context, "RecordTaskPlan")
            try:
                payload = json.loads(request.plan_json)
                state.registry.record_task_plan(payload)
                return self._task_reply({"found": True, "task_id": payload["task_id"]})
            except Exception as exc:
                return self._task_reply(error=str(exc))

        async def RecordTaskStep(self, request, context):
            await _auth(context, "RecordTaskStep")
            try:
                payload = json.loads(request.payload_json)
                if request.kind == "intent":
                    state.registry.record_task_intent(request.task_id, request.revision, request.step_id, payload)
                elif request.kind == "result":
                    state.registry.record_task_result(request.task_id, request.revision, request.step_id, payload)
                else:
                    raise ValueError("unknown_task_event_kind")
                return self._task_reply({"found": True, "task_id": request.task_id})
            except Exception as exc:
                return self._task_reply(error=str(exc))

        async def FinishTask(self, request, context):
            await _auth(context, "FinishTask")
            try:
                state.registry.finish_task(request.task_id, interrupted=request.interrupted)
                return self._task_reply(state.registry.get_task(request.task_id))
            except Exception as exc:
                return self._task_reply(error=str(exc))

        async def GetTask(self, request, context):
            await _auth(context, "GetTask")
            try:
                return self._task_reply(state.registry.get_task(request.task_id))
            except Exception as exc:
                return self._task_reply(error=str(exc))

        async def ReconcileTask(self, request, context):
            await _auth(context, "ReconcileTask")
            try:
                return self._task_reply(await state.registry.reconcile_task(request.task_id))
            except Exception as exc:
                return self._task_reply(error=str(exc))

        async def ListTasks(self, request, context):
            await _auth(context, "ListTasks")
            try:
                return pb2.TaskListReply(tasks_json=json.dumps(state.registry.list_tasks(request.limit or 20), ensure_ascii=False))
            except Exception as exc:
                return pb2.TaskListReply(error=str(exc))

        async def CancelOperation(self, request, context):
            principal = await _auth(context, "CancelOperation")
            # 遵循 D028：先撤权取消，审计写入失败不得阻断取消本身。
            # Follow D028: revoke and cancel first; audit persistence failure may never block cancellation.
            record = state.registry.cancel_operation(request.operation_id)
            if principal is not None and record is not None:
                try:
                    record_rpc_audit(state.registry, record, "rpc_operator_action_finished",
                                     _audit_body(principal, "cancel", "CancelOperation", record, status="completed"))
                except Exception as exc:
                    logger.error("rpc cancel audit failed: operation=%s error=%s", record.operation_id, type(exc).__name__)
            return self._operation_reply(record)

        async def CheckEffect(self, request, context):
            await _auth(context, "CheckEffect")
            result = await state.registry.check_effect_async(request.operation_id, request.attempt_id,
                                                             request.evidence_id, request.verification_digest)
            evidence = result.pop("evidence")
            return pb2.EffectReply(**result, evidence_json=json.dumps(evidence) if evidence else "")

        async def ReconcileEffect(self, request, context):
            principal = await _auth(context, "ReconcileEffect")
            audit_record = None
            audit_body = None
            try:
                if principal is not None:
                    # 预检目标后再写开始审计；审计失败拒绝采样。 / Pre-check target before start audit; fail closed on DB error.
                    audit_record, _, lease = state.registry._reserve_effect(request.operation_id, request.attempt_id)
                    state.registry.arbiter.release(lease)
                    audit_body = _audit_body(principal, "reconcile_effect", "ReconcileEffect", audit_record)
                    try:
                        record_rpc_audit(state.registry, audit_record, "rpc_operator_action_started", audit_body)
                    except (sqlite3.DatabaseError, RuntimeError):
                        reply = self._operation_reply(state.registry.get_operation(request.operation_id))
                        reply.reconciliation_error = "rpc_audit_unavailable"
                        return reply
                updated = await state.registry.reconcile_effect_async(request.operation_id, request.attempt_id)
                if principal is not None and audit_record is not None and audit_body is not None:
                    try:
                        record_rpc_audit(state.registry, updated, "rpc_operator_action_finished", dict(
                            audit_body, status="completed",
                            effect_evidence_id=updated.effect_evidence["evidence_id"],
                            effect_verdict=updated.effect_evidence["verdict"],
                        ))
                    except (sqlite3.DatabaseError, RuntimeError):
                        reply = self._operation_reply(updated)
                        reply.reconciliation_error = "rpc_audit_unavailable"
                        return reply
                return self._operation_reply(updated)
            except EffectRejected as exc:
                reason = str(exc)
                error_name = type(exc).__name__
            except Exception as exc:
                reason = "effect_reconciliation_failed"
                error_name = type(exc).__name__
            if principal is not None and audit_record is not None and audit_body is not None:
                try:
                    record_rpc_audit(state.registry, audit_record, "rpc_operator_action_finished",
                                     dict(audit_body, status="failed", error=error_name))
                except Exception:
                    pass
            reply = self._operation_reply(state.registry.get_operation(request.operation_id))
            reply.reconciliation_error = reason
            return reply

        async def ReconcileStop(self, request, context):
            principal = await _auth(context, "ReconcileStop")
            audit_record = None
            audit_body = None
            try:
                if principal is not None:
                    if request.confirmed is not True:
                        raise ConfirmationRequired("maintenance.reconcile_stop")
                    existing = state.registry.get_operation(request.operation_id)
                    if (existing is not None and existing.attempt_id == request.attempt_id
                            and existing.state == "OUTCOME_UNKNOWN"
                            and existing.stop_state == "MOTION_STOPPED_OBSERVED"):
                        return self._operation_reply(existing)
                    audit_record, _, profile = state.registry._validate_stop_reconciliation(
                        request.operation_id, request.attempt_id, allow_already_observed=False, check_lock=True,
                    )
                    audit_body = _audit_body(
                        principal, "reconcile_stop", "ReconcileStop", audit_record,
                        confirmed=True, stop_profile_digest=profile.digest, guard_halted=_guard_halted(),
                    )
                    # 先持久化操作者停止意图，再允许控制器进入维护围栏。
                    # Persist operator stop intent before allowing the controller to enter the maintenance fence.
                    try:
                        record_rpc_audit(state.registry, audit_record, "rpc_operator_action_started", audit_body)
                    except (sqlite3.DatabaseError, RuntimeError):
                        reply = self._operation_reply(state.registry.get_operation(request.operation_id))
                        reply.reconciliation_error = "rpc_audit_unavailable"
                        return reply
                    updated = await state.registry.reconcile_stop(
                        request.operation_id, request.attempt_id, confirmed=True, allow_already_observed=False,
                    )
                    try:
                        record_rpc_audit(state.registry, updated, "rpc_operator_action_finished", dict(
                            audit_body, status="completed", stop_state=updated.stop_state,
                            stop_nonce=updated.stop_evidence["nonce"],
                            stop_profile_digest=updated.stop_evidence["profile_digest"],
                            gripper_strategy=updated.stop_evidence["gripper_strategy"],
                            guard_halted=_guard_halted(),
                        ))
                    except (sqlite3.DatabaseError, RuntimeError):
                        reply = self._operation_reply(updated)
                        reply.reconciliation_error = "rpc_audit_unavailable"
                        return reply
                    return self._operation_reply(updated)
                record = await state.registry.reconcile_stop(
                    request.operation_id, request.attempt_id, confirmed=request.confirmed,
                )
                return self._operation_reply(record)
            except ConfirmationRequired as exc:
                reason = "confirmation_required"
                error_name = type(exc).__name__
            except StopRejected as exc:
                reason = str(exc)
                error_name = type(exc).__name__
            except Exception as exc:
                reason = "stop_reconciliation_failed"
                error_name = type(exc).__name__
            if principal is not None and audit_record is not None and audit_body is not None:
                try:
                    record_rpc_audit(state.registry, audit_record, "rpc_operator_action_finished",
                                     dict(audit_body, status="failed", error=error_name))
                except Exception:
                    pass
            reply = self._operation_reply(state.registry.get_operation(request.operation_id))
            reply.reconciliation_error = reason
            return reply

        async def ListSkills(self, request, context):
            await _auth(context, "ListSkills")
            reply = pb2.ListSkillsReply()
            for m in state.registry.catalog():
                reply.skills.append(pb2.SkillInfo(manifest_json=m.model_dump_json()))
            return reply

        async def InvokeSkill(self, request, context):
            await _auth(context, "InvokeSkill")

            def ev(phase, detail="", error_code="", data=None, result=None):
                e = pb2.SkillEvent(command_id=request.command_id, phase=phase, detail=detail)
                e.operation_id = request.operation_id or request.command_id
                if result is not None:
                    e.operation_id = result.operation_id or e.operation_id
                    e.attempt_id = result.attempt_id
                    e.invocation_state = result.invocation_state
                    e.stop_state = result.stop_state
                if error_code:
                    e.error.code = error_code
                    e.error.message = detail
                if data:
                    e.data_json = json.dumps(data, ensure_ascii=False, default=str)
                return e

            if state.halted or not state.supervision_ok():
                reason = state.halt_reason or "supervisor_link_down"
                if not state.halted and state.require_supervisor:
                    # 监督缺席不仅拒绝本次调用，还闩锁后续执行。
                    state.halt(reason)  # absent supervision latches, not just rejects
                yield ev(pb2.SkillEvent.HALTED, f"halted: {reason}", "halted")
                return
            try:
                params = json.loads(request.params_json) if request.params_json else {}
                identity = OperationIdentity(
                    api_version=request.api_version or "embodied.dev/v1alpha1",
                    task_id=request.task_id or request.command_id,
                    operation_id=request.operation_id or request.command_id,
                    plan_revision=request.plan_revision or 1,
                )
            except (ValueError, TypeError):
                yield ev(pb2.SkillEvent.FAILED, "bad params or operation identity", "bad_request")
                return
            yield ev(pb2.SkillEvent.ACCEPTED)
            try:
                # 独立任务承载调用取消，保留 RPC 协程发送终态。 / Cancel the invocation task while keeping RPC alive to report its terminal state.
                invocation = asyncio.create_task(state.registry.invoke(
                    request.skill, params, confirmed=bool(request.confirmed), operation=identity,
                ))
                result = await invocation
            except asyncio.CancelledError:
                if context.cancelled():
                    raise
                record = state.registry.get_operation(identity.operation_id)
                if record is None:
                    yield ev(pb2.SkillEvent.FAILED, "cancelled before admission", "cancelled")
                    return
                result = state.registry._from_record(record)
                yield ev(pb2.SkillEvent.HALTED if state.halted else pb2.SkillEvent.FAILED,
                         "cancel requested; physical stop unconfirmed", "outcome_unknown", result.data, result)
                return
            except ConfirmationRequired:
                yield ev(pb2.SkillEvent.FAILED, "confirmation required", "need_confirm")
                return
            except Exception as e:
                yield ev(pb2.SkillEvent.FAILED, f"{type(e).__name__}: {e}", "invoke_error")
                return
            # 停止与运动竞争时如实报告停止。
            if state.halted:  # halt raced the motion: report honestly
                yield ev(pb2.SkillEvent.HALTED, f"halted during execution: {state.halt_reason}", "halted",
                         result.data, result)
                return
            phase = pb2.SkillEvent.SUCCEEDED if result.ok else pb2.SkillEvent.FAILED
            yield ev(phase, result.detail, "" if result.ok else "skill_failed", data=result.data, result=result)

        async def StreamState(self, request, context):
            await _auth(context, "StreamState")
            interval = max(0.02, float(request.min_interval_ms or 100) / 1000.0)
            while not context.cancelled():
                if state.sim is not None:
                    try:
                        obs = await state.registry.observe(state.sim.read)
                    except ObservationUnavailable:
                        await asyncio.sleep(interval)
                        continue
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
            await _auth(context, "Halt")
            state.halt(request.reason or "halt_requested")
            return pb2.HaltReply(halted=True)

        async def Reset(self, request, context):
            principal = await _auth(context, "Reset")
            state.reset(request.reason or "reset_requested", principal=principal)
            return pb2.ResetReply(reset=True)

        async def SupervisorLink(self, request_iterator, context):
            # 必须在修改连接标志与进入 finally 前鉴权，避免未授权调用篡改监督状态。
            # Authenticate before touching connection flags or entering finally so unauthorized callers cannot tamper liveness.
            await _auth(context, "SupervisorLink")
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
    (e.g. frozen process: TCP stays up, pings stop).

    监督流未关闭但心跳过期也闩锁，例如 TCP 仍连接而进程冻结。
    """
    while True:
        if (
            state.require_supervisor and state.supervisor_connected
            and (time.monotonic() - state.last_supervisor_ping) >= SUPERVISOR_STALE_S
            and not state.halted
        ):
            state.halt("supervisor_stale")
        await asyncio.sleep(interval_s)


async def serve(
    state: ControlState, port: int = DEFAULT_PORT, host: str = "127.0.0.1",
    *, tls: RpcTlsPaths | None = None, policy: RpcAccessPolicy | None = None,
):
    if (tls is None) != (policy is None):
        raise ValueError("rpc_tls_and_policy_must_be_paired")
    if not is_loopback_host(host) and tls is None:
        raise ValueError("non_loopback_rpc_requires_mtls_policy")
    _, pb2_grpc = import_control()
    server = grpc.aio.server()
    if tls is not None and policy is not None:
        creds = load_server_credentials(tls, policy)
        state.authorizer = RpcAuthorizer(policy, "control")
        pb2_grpc.add_ControlServiceServicer_to_server(build_servicer(state), server)
        bound = server.add_secure_port(f"{host}:{port}", creds)
    else:
        pb2_grpc.add_ControlServiceServicer_to_server(build_servicer(state), server)
        bound = server.add_insecure_port(f"{host}:{port}")
    await server.start()
    logger.info("control service on %s:%d (require_supervisor=%s, mtls=%s)",
                host, bound, state.require_supervisor, tls is not None)
    return server, bound
