"""Isolated physical control embodiment running MuJoCo physics in a child process (D034).

隔离物理控制本体适配器（D034）：将 MuJoCo 物理仿真、逆解、驱动 Guard 与停止观测迁入独立子进程；
父进程仅持有镜像 Guard、ActionAuthority、渲染计算监督器与控制生命周期，不直接执行物理步进。
"""

from __future__ import annotations

import base64
import threading
import time
from dataclasses import asdict
from typing import Any, Callable

import numpy as np

from embodied.contracts.action import ActionEnvelope, ControlObservationRef
from embodied.contracts.compute import decode_array, json_payload
from embodied.contracts.stopping import (
    StopEvidence,
    StopProfile,
    StopRejected,
    StopRequest,
    validate_stop_evidence,
)
from embodied.control.actions import ActionAuthority, ActionRejected
from embodied.control.camera import CameraModel
from embodied.control.hal import (
    ActionCommand,
    EmbodimentSpec,
    JointSpec,
    Observation,
    Pose,
    WriteResult,
)
from embodied.control.kinematics import IKResult
from embodied.control.simassets import stage_tabletop
from embodied.control.stopping import _asset_digest
from embodied.runtime.compute import ComputeProcess, ComputeUnavailable
from embodied.runtime.control_process import ControlProcess, PhysicalControlUnavailable
from embodied.runtime.invocation import CURRENT_INVOCATION, OBSERVATION_ONLY, invocation_access
from embodied.safety.guard import Guard, GuardEvent, GuardLimits

StepHook = Callable[[Observation, ActionCommand | None], None]
_STEP_CHUNK = 25


def _deser_obs(raw: dict[str, Any]) -> Observation:
    objects = {
        str(name): Pose(
            pos=(float(item["pos"][0]), float(item["pos"][1]), float(item["pos"][2])),
            quat=(float(item["quat"][0]), float(item["quat"][1]), float(item["quat"][2]), float(item["quat"][3])),
        )
        for name, item in raw.get("objects", {}).items()
    }
    extras = dict(raw.get("extras", {}))
    if "regions" in extras and isinstance(extras["regions"], dict):
        extras["regions"] = {
            str(rname): {
                "center": tuple(float(v) for v in rdata["center"]),
                "half": tuple(float(v) for v in rdata["half"]),
            }
            for rname, rdata in extras["regions"].items()
        }
    ee = raw["ee_pose"]
    return Observation(
        t=float(raw["t"]),
        qpos=tuple(float(v) for v in raw["qpos"]),
        qvel=tuple(float(v) for v in raw["qvel"]),
        gripper_opening=float(raw["gripper_opening"]),
        ee_pose=Pose(
            pos=(float(ee["pos"][0]), float(ee["pos"][1]), float(ee["pos"][2])),
            quat=(float(ee["quat"][0]), float(ee["quat"][1]), float(ee["quat"][2]), float(ee["quat"][3])),
        ),
        objects=objects,
        extras=extras,
    )


def _deser_spec(raw: dict[str, Any]) -> EmbodimentSpec:
    joints = tuple(
        JointSpec(name=str(j["name"]), lower=float(j["lower"]), upper=float(j["upper"]))
        for j in raw["joints"]
    )
    gj = raw.get("gripper_joint")
    gripper = None if gj is None else JointSpec(name=str(gj["name"]), lower=float(gj["lower"]), upper=float(gj["upper"]))
    return EmbodimentSpec(embodiment_id=str(raw["embodiment_id"]), joints=joints, gripper_joint=gripper)


def _serialize_limits(limits: GuardLimits | None) -> dict[str, Any] | None:
    if limits is None:
        return None
    data = asdict(limits)
    data["joint_lower"] = list(limits.joint_lower)
    data["joint_upper"] = list(limits.joint_upper)
    data["ee_fence_min"] = list(limits.ee_fence_min)
    data["ee_fence_max"] = list(limits.ee_fence_max)
    data["allowed_sources"] = sorted(limits.allowed_sources)
    return data


def _deserialize_limits(raw: dict[str, Any]) -> GuardLimits:
    return GuardLimits(
        joint_lower=tuple(float(v) for v in raw["joint_lower"]),
        joint_upper=tuple(float(v) for v in raw["joint_upper"]),
        max_delta_per_write=float(raw["max_delta_per_write"]),
        ee_fence_min=tuple(float(v) for v in raw["ee_fence_min"]),
        ee_fence_max=tuple(float(v) for v in raw["ee_fence_max"]),
        allowed_sources=frozenset(str(s) for s in raw["allowed_sources"]),
    )


class _IsolatedGuard(Guard):
    """Parent-side Guard mirror synchronized with the isolated physics worker.

    父进程侧 Guard 镜像：急停优先本地闩锁（D028）再同步/强杀子进程；复位须子进程 READY 且确认。
    """

    def __init__(
        self,
        limits: GuardLimits,
        owner: "IsolatedTabletopSim",
        on_event: Callable[[GuardEvent], None] | None = None,
    ) -> None:
        super().__init__(limits, on_event=on_event)
        self._owner = owner

    def estop(self, reason: str = "estop") -> None:
        # 先在父进程同步闩锁，确保任何子进程卡死或存储故障均无法阻断急停（D028）。
        # Latch locally first so a hung worker or storage failure can never block estop (D028).
        super().estop(reason)
        self._owner._sync_estop(reason)

    def _latch_local_only(self, reason: str, kind: str = "estop") -> None:
        if not self.halted:
            self._latch(reason, kind=kind)
        else:
            # 保持已闩锁状态并刷新代次，确保审查快照能感知物理进程故障。
            # Keep latched state and bump generation so recovery review detects the worker fault.
            self.generation += 1

    def reset(self) -> None:
        self._owner._sync_guard_reset()
        super().reset()

    def _sync_from_worker(self, reply: dict[str, Any]) -> None:
        for ev in reply.get("guard_events", ()):
            self._emit(GuardEvent(kind=str(ev["kind"]), reason=str(ev["reason"]), detail=dict(ev.get("detail", {}))))
        if reply.get("guard_halted") and not self.halted:
            self._latch(str(reply.get("guard_halt_reason") or "worker_guard_halted"), kind="halted")


class IsolatedStopController:
    """Stop controller delegating MuJoCo hold/contact observation to the physics worker.

    将 MuJoCo 保持与接触力采样委派给隔离物理子进程，并在父进程时钟上独立复核停止证据。
    """

    def __init__(self, sim: "IsolatedTabletopSim", profile: StopProfile) -> None:
        self.sim = sim
        self.profile = profile
        self._owner: str | None = None
        self._issued: StopEvidence | None = None

    def prepare(self, request: StopRequest) -> StopEvidence:
        profile, sim = self.profile, self.sim
        if request.robot_id != profile.robot_id or set(request.resources) != set(profile.resources):
            raise StopRejected("stop_controller_binding_mismatch")
        if sim.control_process_state != "READY":
            raise StopRejected("physical_control_unavailable")
        started = time.monotonic()
        remaining = request.deadline_monotonic - started
        if remaining <= 0:
            raise StopRejected("stop_request_expired")
        if not sim._state_lock.acquire(timeout=min(remaining, profile.max_wall_duration_s)):
            raise StopRejected("stop_controller_busy")
        try:
            now = time.monotonic()
            remaining = request.deadline_monotonic - now
            if remaining <= 0:
                raise StopRejected("stop_request_expired")
            if sim._stop_fence and self._owner != request.operation_id:
                raise StopRejected("other_stop_reconciliation_pending")
            sim._stop_fence = request.nonce
            sim.action_controller.invalidate()
            self._owner = request.operation_id
            self._issued = None
            try:
                reply = sim._control_process.call(
                    "stop_prepare",
                    {"request": request.model_dump(mode="json"), "remaining_s": remaining},
                    timeout_s=min(remaining, profile.max_wall_duration_s) + 0.05,
                )
            except PhysicalControlUnavailable as exc:
                raise StopRejected(f"physical_control_unavailable:{exc}") from None
            if reply.get("status") == "rejected":
                raise StopRejected(str(reply.get("reason", "stop_rejected")))
            if reply.get("status") != "ok":
                raise StopRejected("invalid_stop_reply")
            completed = time.monotonic()
            if completed > request.deadline_monotonic:
                raise StopRejected("stop_observation_timeout")
            raw_ev = dict(reply["evidence"])
            # 使用父进程单调时钟重新锚定完成时刻与截止时间，绝不盲信子进程单调钟。
            # Anchor measured_at_monotonic and deadline to the parent clock; never trust child monotonic time blindly.
            raw_ev["deadline_monotonic"] = request.deadline_monotonic
            raw_ev["measured_at_monotonic"] = completed
            evidence = StopEvidence.model_validate_json(json_payload(raw_ev))
            if evidence.scene_epoch != sim.state_epoch:
                raise StopRejected("stop_scene_changed")
            validate_stop_evidence(evidence, request, profile)
            self._issued = evidence
            return evidence
        finally:
            sim._state_lock.release()

    def commit(self, evidence: StopEvidence, request: StopRequest, persist):
        """Persist stop evidence first, then release child and parent fences.

        先在维护锁内事务落账停止证据，再释放子进程与父进程维护围栏。
        """
        with self.sim._state_lock:
            if self._issued is not evidence or self.sim._stop_fence != evidence.nonce:
                raise StopRejected("stop_evidence_not_issued_here")
            if self.sim.state_epoch != evidence.scene_epoch:
                raise StopRejected("stop_scene_changed")
            if self.sim.control_process_state != "READY":
                raise StopRejected("physical_control_unavailable")
            validate_stop_evidence(evidence, request, self.profile)
            result = persist()
            reply = self.sim._control_process.call(
                "stop_commit",
                {"nonce": evidence.nonce, "scene_epoch": evidence.scene_epoch},
            )
            if reply.get("status") != "ok":
                raise StopRejected(str(reply.get("reason", "stop_commit_rejected")))
            self.sim._stop_fence = None
            self._owner = None
            self._issued = None
            return result


class IsolatedTabletopSim:
    """Isolated MuJoCo tabletop embodiment matching TabletopSim's public surface.

    与 TabletopSim 保持相同公开接口的隔离物理控制本体：物理仿真运行于独立无头子进程，
    离屏渲染仍独立运行于 D026 RenderBackend 子进程。
    """

    def __init__(
        self,
        *,
        guard: Guard | None = None,
        seed: int = 0,
        on_step: StepHook | None = None,
        on_guard_event: Callable[[GuardEvent], None] | None = None,
        spawn_radial: tuple[float, float] = (0.18, 0.27),
        spawn_angle_deg: tuple[float, float] = (230.0, 310.0),
        control_timeout_s: float = 5.0,
        startup_timeout_s: float = 30.0,
        cancel_grace_s: float = 0.15,
        halt_timeout_s: float = 0.5,
        _control_command: list[str] | None = None,
    ) -> None:
        self._state_lock = threading.RLock()
        self._stop_fence: str | None = None
        self._action_controller: ActionAuthority | None = None
        self._render_process: ComputeProcess | None = None
        self._render_process_lock = threading.Lock()
        self._on_step = on_step
        self._last_cmd: ActionCommand | None = None

        scene = stage_tabletop()
        self._scene_path = scene
        self._asset_digest = _asset_digest.__wrapped__(str(scene))
        self._seed = int(seed)
        self._spawn_radial = (float(spawn_radial[0]), float(spawn_radial[1]))
        self._spawn_angle_deg = (float(spawn_angle_deg[0]), float(spawn_angle_deg[1]))

        limits_payload = _serialize_limits(guard.limits if guard is not None else None)
        config = {
            "scene": str(scene),
            "asset_digest": self._asset_digest,
            "seed": self._seed,
            "spawn_radial": list(self._spawn_radial),
            "spawn_angle_deg": list(self._spawn_angle_deg),
            "limits": limits_payload,
        }
        self._control_process = ControlProcess(
            config,
            timeout_s=control_timeout_s,
            startup_timeout_s=startup_timeout_s,
            cancel_grace_s=cancel_grace_s,
            halt_timeout_s=halt_timeout_s,
            on_fault=self._on_control_fault,
            _command=_control_command,
        )
        metadata = self._control_process.start()
        self._spec = _deser_spec(metadata["spec"])
        self._timestep = float(metadata["timestep"])
        self._home_qpos = np.asarray(metadata["home_qpos"], dtype=float)
        self._nq = int(metadata["nq"])
        self._nv = int(metadata["nv"])
        self.state_epoch = str(metadata["state_epoch"])

        profile = StopProfile.model_validate_json(json_payload(metadata["stop_profile"]))
        if profile.asset_digest != self._asset_digest:
            self._control_process.close()
            raise PhysicalControlUnavailable("physical_control_profile_mismatch")
        self._stop_controller = IsolatedStopController(self, profile)

        limits = guard.limits if guard is not None else _deserialize_limits(metadata["limits"])
        listener = on_guard_event if on_guard_event is not None else (guard._on_event if guard is not None else None)
        self.guard = _IsolatedGuard(limits, self, on_event=listener)
        self.guard.halted = bool(metadata.get("guard_halted", False))
        self.guard.halt_reason = str(metadata.get("guard_halt_reason", ""))
        self.guard.generation = int(metadata.get("guard_generation", 0))
        if guard is not None and guard.halted:
            self.guard.estop(guard.halt_reason or "estop")

    def _on_control_fault(self, reason: str) -> None:
        if hasattr(self, "guard"):
            self.guard._latch_local_only(f"physical_control_fault:{reason}", kind="estop")

    def _sync_estop(self, reason: str) -> None:
        if hasattr(self, "_control_process"):
            self._control_process.urgent_estop(reason)

    def _sync_guard_reset(self) -> None:
        if self._control_process.state != "READY":
            raise PhysicalControlUnavailable("physical_control_unavailable")
        self._control_process.call("guard_reset", {})

    @property
    def control_process_state(self) -> str:
        return self._control_process.state

    @property
    def control_pid(self) -> int | None:
        return self._control_process.last_pid

    @property
    def control_boot_id(self) -> str:
        return self._control_process.boot_id

    @property
    def timestep(self) -> float:
        return self._timestep

    @property
    def stop_controller(self) -> IsolatedStopController:
        return self._stop_controller

    @property
    def action_controller(self) -> ActionAuthority:
        if self._action_controller is None:
            self._action_controller = ActionAuthority(self)
            self._action_controller.boot_id = self._control_process.boot_id
        return self._action_controller

    def spec(self) -> EmbodimentSpec:
        return self._spec

    def _read_state(self) -> Observation:
        with self._state_lock:
            reply = self._control_process.call("read", {})
            self.state_epoch = str(reply["state_epoch"])
            self.guard._sync_from_worker(reply)
            return _deser_obs(reply["obs"])

    def read(self) -> Observation:
        with self._state_lock:
            observation = self._read_state()
            observation.reference = self.action_controller.observe(observation.t)
            return observation

    def read_control_feedback(self) -> Observation:
        with self._state_lock:
            reply = self._control_process.call("read_control_feedback", {})
            self.state_epoch = str(reply["state_epoch"])
            self.guard._sync_from_worker(reply)
            observation = _deser_obs(reply["obs"])
            observation.reference = self.action_controller.observe(observation.t)
            return observation

    def read_reference_state(self) -> tuple[str, Observation]:
        with self._state_lock:
            obs = self.read()
            return self.state_epoch, obs

    def prepare_action(self, cmd: ActionCommand, reference: ControlObservationRef | None = None) -> ActionEnvelope:
        with self._state_lock:
            return self.action_controller.make(cmd, reference)

    def reject_action(self, reason: str) -> WriteResult:
        with self._state_lock:
            permit = CURRENT_INVOCATION.get()
            if permit is not None:
                permit.reject(reason)
            return WriteResult(applied=False, reason=reason)

    def write(self, cmd: ActionCommand) -> WriteResult:
        with self._state_lock:
            if self.action_controller.runtime is not None:
                try:
                    envelope = cmd.envelope or self.prepare_action(cmd)
                    return self.write_envelope(envelope)
                except (ActionRejected, ValueError) as exc:
                    return self.reject_action(str(exc))
            if self._stop_fence:
                return WriteResult(applied=False, reason="stop_reconciliation_pending")
            if self.guard.halted:
                return self.guard._deny(f"halted:{self.guard.halt_reason}")
            # 绝不跨阻塞 IPC 持有 permit._lock，否则父进程 cancel_all()/revoke() 会被卡死子进程阻塞。
            # Never hold permit._lock across blocking IPC, or parent cancel_all()/revoke() would block on a hung child.
            with invocation_access() as allowed:
                if not allowed:
                    return WriteResult(applied=False, reason="invocation_revoked_or_expired")
            try:
                reply = self._control_process.call(
                    "write",
                    {
                        "joint_targets": [float(v) for v in cmd.joint_targets],
                        "gripper": None if cmd.gripper is None else float(cmd.gripper),
                        "source": str(cmd.source),
                    },
                )
            except PhysicalControlUnavailable as exc:
                return WriteResult(applied=False, reason=str(exc))
            self.state_epoch = str(reply["state_epoch"])
            self.guard._sync_from_worker(reply)
            if reply["applied"] and reply.get("applied_cmd") is not None:
                ac = reply["applied_cmd"]
                self._last_cmd = ActionCommand(
                    joint_targets=tuple(float(v) for v in ac["joint_targets"]),
                    gripper=ac["gripper"],
                    source=str(ac["source"]),
                )
            return WriteResult(
                applied=bool(reply["applied"]),
                reason=str(reply.get("reason", "")),
                clamped=bool(reply.get("clamped", False)),
            )

    def write_envelope(self, envelope: ActionEnvelope) -> WriteResult:
        with self._state_lock:
            if self._stop_fence:
                return self.reject_action("stop_reconciliation_pending")
            if self.guard.halted:
                self.guard._deny(f"halted:{self.guard.halt_reason}")
                return self.reject_action(f"halted:{self.guard.halt_reason}")
            # 原子校验证件与动作包络后立即释放 permit._lock，再执行有界物理 IPC。
            # Validate permit and envelope atomically, release permit._lock, then execute bounded physical IPC.
            with invocation_access() as allowed:
                if not allowed:
                    return self.reject_action("invocation_revoked_or_expired")
                try:
                    envelope = self.action_controller.validate(envelope, CURRENT_INVOCATION.get())
                except (ActionRejected, ValueError) as exc:
                    return self.reject_action(str(exc))
            try:
                reply = self._control_process.call(
                    "write",
                    {
                        "joint_targets": [float(v) for v in envelope.joint_targets],
                        "gripper": None if envelope.gripper is None else float(envelope.gripper),
                        "source": str(envelope.source),
                    },
                )
            except PhysicalControlUnavailable as exc:
                return self.reject_action(str(exc))
            self.state_epoch = str(reply["state_epoch"])
            self.guard._sync_from_worker(reply)
            if not reply["applied"]:
                return self.reject_action(str(reply.get("reason", "guard_denied")))
            ac = reply.get("applied_cmd")
            applied = ActionCommand(
                joint_targets=tuple(float(v) for v in ac["joint_targets"]) if ac else envelope.joint_targets,
                gripper=ac["gripper"] if ac else envelope.gripper,
                source=str(ac["source"]) if ac else envelope.source,
                envelope=envelope,
            )
            permit = CURRENT_INVOCATION.get()
            if permit is not None:
                permit.action_deadline = envelope.valid_until_monotonic
            self._last_cmd = applied
            return WriteResult(applied=True, clamped=bool(reply.get("clamped", False)))

    def step(self, n: int = 1) -> None:
        with self._state_lock:
            if self._stop_fence or self.guard.halted:
                permit = CURRENT_INVOCATION.get()
                if permit is not None:
                    permit.reject("controller_halted_or_fenced")
                return
            remaining = int(n)
            final_obs: Observation | None = None
            while remaining > 0:
                chunk = min(remaining, _STEP_CHUNK)
                want_obs = self._on_step is not None and chunk == remaining
                with invocation_access() as allowed:
                    if not allowed:
                        return
                    if (
                        self.action_controller.runtime is not None
                        and not self.action_controller.can_step(CURRENT_INVOCATION.get())
                    ):
                        return
                try:
                    reply = self._control_process.call("step", {"n": chunk, "return_obs": want_obs})
                except PhysicalControlUnavailable as exc:
                    permit = CURRENT_INVOCATION.get()
                    if permit is not None:
                        permit.reject(str(exc))
                        return
                    raise
                self.state_epoch = str(reply["state_epoch"])
                self.guard._sync_from_worker(reply)
                if want_obs and reply.get("obs") is not None:
                    final_obs = _deser_obs(reply["obs"])
                    final_obs.reference = self.action_controller.observe(final_obs.t)
                remaining -= chunk
            if self._on_step is not None and final_obs is not None:
                self._on_step(final_obs, self._last_cmd)

    def reset(self, *, randomize: bool = False) -> Observation:
        if OBSERVATION_ONLY.get():
            raise RuntimeError("observation_is_read_only")
        controller = self._action_controller
        if controller is not None and controller.runtime is not None and controller.runtime.observation_pending:
            raise ActionRejected("observation_worker_busy")
        with self._state_lock:
            if self._stop_fence:
                raise RuntimeError("stop_reconciliation_pending")
            if self._action_controller is not None:
                self._action_controller.require_idle()
                self._action_controller.invalidate()
            reply = self._control_process.call("reset", {"randomize": bool(randomize)})
            if reply.get("status") == "rejected":
                raise RuntimeError(str(reply.get("reason", "reset_rejected")))
            self.state_epoch = str(reply["state_epoch"])
            self.guard.halted = bool(reply.get("guard_halted", False))
            self.guard.halt_reason = str(reply.get("guard_halt_reason", ""))
            self.guard.generation += 1
            self._last_cmd = None
            obs = _deser_obs(reply["obs"])
            obs.reference = self.action_controller.observe(obs.t)
            return obs

    def set_hooks(
        self,
        on_step: StepHook | None = None,
        on_guard_event: Callable[[GuardEvent], None] | None = None,
    ) -> None:
        with self._state_lock:
            self._on_step = on_step
            self.guard.set_listener(on_guard_event)

    def arm_qpos(self) -> np.ndarray:
        return np.asarray(self.read_control_feedback().qpos, dtype=float)

    def home_qpos(self) -> np.ndarray:
        return self._home_qpos.copy()

    def solve_reach(
        self, target_pos: Any, *, q_init: np.ndarray | None = None, approach_down: bool = True
    ) -> IKResult:
        with self._state_lock:
            reply = self._control_process.call(
                "solve_reach",
                {
                    "target_pos": [float(v) for v in np.asarray(target_pos, dtype=float)],
                    "q_init": None if q_init is None else [float(v) for v in np.asarray(q_init, dtype=float)],
                    "approach_down": bool(approach_down),
                },
            )
            return IKResult(
                qpos=np.asarray(reply["qpos"], dtype=float),
                pos_err=float(reply["pos_err"]),
                axis_err=float(reply["axis_err"]),
                converged=bool(reply["converged"]),
                iters=int(reply["iters"]),
            )

    def object_pos(self, name: str) -> np.ndarray:
        return np.asarray(self._read_state().objects[name].pos, dtype=float)

    def regions(self) -> dict[str, dict[str, tuple[float, float, float]]]:
        return dict(self._read_state().extras.get("regions", {}))

    # 独立离屏渲染子进程（复用 D026 RenderBackend，与物理控制进程完全隔离）。
    # -- isolated rendering (reuses D026 RenderBackend, separate from physics worker) --

    def prepare_renderer(self) -> None:
        with self._render_process_lock:
            if self._render_process is None:
                self._render_process = ComputeProcess({
                    "backend": "render",
                    "scene": str(self._scene_path),
                    "asset_digest": self._asset_digest,
                })
            worker = self._render_process
        metadata = worker.start()
        if metadata.get("nq") != self._nq or metadata.get("nv") != self._nv:
            worker.close()
            raise ComputeUnavailable("render_model_mismatch")

    def _render_copy(self, camera: str, width: int, height: int, depth: bool, *, jpeg: bool = False):
        if type(width) is not int or type(height) is not int or not 0 < width <= 1024 or not 0 < height <= 1024:
            raise ValueError("invalid render dimensions")
        self.prepare_renderer()
        with self._state_lock:
            snap = self._control_process.call("render_snapshot", {"camera": str(camera)})
            if snap.get("status") == "rejected":
                raise ValueError(str(snap.get("reason", "unknown camera")))
            epoch = str(snap["epoch"])
            self.state_epoch = epoch
            state = snap["state"]

        def validate(reply):
            expected = (
                {"epoch", "time", "jpeg"}
                if jpeg
                else {"epoch", "time", "rgb", "depth"}
                if depth
                else {"epoch", "time", "rgb"}
            )
            if (
                set(reply) != expected
                or reply["epoch"] != epoch
                or type(reply["time"]) not in (float, int)
                or reply["time"] != state["time"]
            ):
                raise ValueError("render_snapshot_mismatch")
            if jpeg:
                raw = base64.b64decode(reply["jpeg"], validate=True)
                if not 4 <= len(raw) <= 4 * 1024 * 1024 or not raw.startswith(b"\xff\xd8") or not raw.endswith(b"\xff\xd9"):
                    raise ValueError("invalid JPEG response")
                return raw
            rgb = decode_array(reply["rgb"], dtype="uint8", shape=(height, width, 3))
            distance = decode_array(reply["depth"], dtype="float32", shape=(height, width)) if depth else None
            return rgb, distance

        assert self._render_process is not None
        result = self._render_process.call(
            "jpeg" if jpeg else "render",
            {"state": state, "camera": camera, "width": width, "height": height, "depth": depth, "epoch": epoch},
            validate=validate,
        )
        with self._state_lock:
            if self.state_epoch != epoch:
                raise ComputeUnavailable("render_scene_changed")
        return result

    def render(self, camera: str = "side", width: int = 640, height: int = 480) -> np.ndarray:
        return self._render_copy(camera, width, height, False)[0]

    def render_rgbd(self, camera: str = "side", width: int = 640, height: int = 480):
        return self._render_copy(camera, width, height, True)

    def render_jpeg(self, camera: str = "side", width: int = 640, height: int = 480) -> bytes:
        return self._render_copy(camera, width, height, False, jpeg=True)

    def camera_model(self, camera: str = "side", width: int = 640, height: int = 480) -> CameraModel:
        with self._state_lock:
            reply = self._control_process.call(
                "camera_model",
                {"camera": str(camera), "width": int(width), "height": int(height)},
            )
            if reply.get("status") == "rejected":
                raise ValueError(str(reply.get("reason", "camera not found")))
            cam = reply["camera"]
            return CameraModel(
                width=int(cam["width"]),
                height=int(cam["height"]),
                fx=float(cam["fx"]),
                fy=float(cam["fy"]),
                cx=float(cam["cx"]),
                cy=float(cam["cy"]),
                pos=(float(cam["pos"][0]), float(cam["pos"][1]), float(cam["pos"][2])),
                rot=tuple(float(v) for v in cam["rot"]),
            )

    def restart_renderer(self) -> None:
        self.action_controller.require_idle()
        if self.action_controller.runtime is not None:
            self.action_controller.runtime.check_thread()
        if self._render_process is not None:
            self._render_process.restart()

    def cancel_render(self) -> None:
        worker = self._render_process
        if worker is not None and worker.busy:
            worker.request_stop()

    def cancel_control(self) -> None:
        """Abort any blocked in-flight physical control call during shutdown.

        停机排空期间中止阻塞中的物理控制调用并强杀卡死子进程。
        """
        if self._control_process.busy:
            self._control_process.request_abort()

    def restart_physical_control(self) -> dict[str, Any]:
        """Explicitly start a fresh physical control worker generation after FAILED.

        仅在空闲、已回收且场景资产摘要未变时启动新物理控制代次：轮换 controller_boot_id 与 scene_epoch，
        并严格保留父进程 Guard 闩锁与账本资源隔离，绝不静默恢复执行。
        """
        runtime = self.action_controller.runtime
        if runtime is not None:
            runtime.check_thread()
            if (
                runtime.observation_pending
                or runtime.stop_pending
                or runtime._active
                or any(not permit.drained for permit in runtime._stop_contexts.values())
            ):
                raise ActionRejected("physical_control_restart_not_idle")
        if CURRENT_INVOCATION.get() is not None:
            raise ActionRejected("physical_control_restart_not_idle")
        if _asset_digest.__wrapped__(str(self._scene_path)) != self._asset_digest:
            raise PhysicalControlUnavailable("physical_control_asset_changed")
        with self._state_lock:
            metadata = self._control_process.restart()
            if int(metadata["nq"]) != self._nq or int(metadata["nv"]) != self._nv:
                self._control_process.close()
                raise PhysicalControlUnavailable("physical_control_model_mismatch")
            profile = StopProfile.model_validate_json(json_payload(metadata["stop_profile"]))
            if profile.digest != self._stop_controller.profile.digest:
                self._control_process.close()
                raise PhysicalControlUnavailable("physical_control_profile_mismatch")
            self.state_epoch = str(metadata["state_epoch"])
            self._stop_fence = None
            self._stop_controller._owner = None
            self._stop_controller._issued = None
            self._last_cmd = None
            # 轮换控制器代次并失效全部旧观察引用，确保旧动作包络与旧效果上下文不可复用。
            # Rotate controller_boot_id and invalidate references so old envelopes and effect contexts fail closed.
            self.action_controller.boot_id = self._control_process.boot_id
            self.action_controller.invalidate()
            # 保留父进程 Guard 闩锁状态：重启物理子进程永不自动清除急停闩锁。
            # Preserve parent Guard latch: restarting the physics worker never clears an estop latch.
            if self.guard.halted:
                self._control_process.call("estop", {"reason": self.guard.halt_reason or "physical_control_fault"})
            return {
                "boot_id": self._control_process.boot_id,
                "pid": self._control_process.last_pid,
                "state_epoch": self.state_epoch,
                "guard_halted": self.guard.halted,
            }

    def close_renderers(self) -> None:
        if self._render_process is not None:
            self._render_process.close()
            self._render_process = None

    def close(self) -> None:
        self.close_renderers()
        if hasattr(self, "_control_process"):
            self._control_process.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
