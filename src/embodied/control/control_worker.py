"""Isolated headless MuJoCo physical control worker (D034); stdout is protocol only.

隔离无头 MuJoCo 物理控制子进程（D034）：仅承接物理仿真、逆解、驱动 Guard 与停止观测，标准输出只用于协议帧。
"""

from __future__ import annotations

import argparse
import contextlib
import math
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import mujoco
import numpy as np

from embodied.contracts.compute import encode_array
from embodied.contracts.control_process import (
    ALLOWED_CONTROL_OPERATIONS,
    ControlWorkerEnvelope,
    encode_control_frame,
    json_payload,
    parse_payload,
    read_control_frame,
)
from embodied.contracts.stopping import StopRejected, StopRequest
from embodied.control.drivers.mujoco_sim import TabletopSim
from embodied.control.hal import ActionCommand, Observation
from embodied.control.render_worker import CAMERA_FIELDS, DATA_FIELDS, POSE_FIELDS
from embodied.control.stopping import _asset_digest
from embodied.runtime.process_tree import bind_parent_death
from embodied.safety.guard import Guard, GuardEvent, GuardLimits


def _ser_obs(obs: Observation) -> dict[str, Any]:
    return {
        "t": float(obs.t),
        "qpos": list(obs.qpos),
        "qvel": list(obs.qvel),
        "gripper_opening": float(obs.gripper_opening),
        "ee_pose": {"pos": list(obs.ee_pose.pos), "quat": list(obs.ee_pose.quat)},
        "objects": {
            name: {"pos": list(pose.pos), "quat": list(pose.quat)}
            for name, pose in obs.objects.items()
        },
        "extras": obs.extras,
    }


def _ser_spec(sim: TabletopSim) -> dict[str, Any]:
    spec = sim.spec()
    return {
        "embodiment_id": spec.embodiment_id,
        "joints": [{"name": j.name, "lower": j.lower, "upper": j.upper} for j in spec.joints],
        "gripper_joint": None
        if spec.gripper_joint is None
        else {
            "name": spec.gripper_joint.name,
            "lower": spec.gripper_joint.lower,
            "upper": spec.gripper_joint.upper,
        },
    }


class PhysicalControlBackend:
    """Headless MuJoCo backend enforcing driver-side Guard and stop observation.

    无头 MuJoCo 后端：在物理进程内独立执行驱动侧 Guard、逆解与停止观测，不加载离屏渲染器。
    """

    def __init__(self, config: dict[str, Any]) -> None:
        required = {"scene", "asset_digest", "seed", "spawn_radial", "spawn_angle_deg", "limits"}
        if set(config) != required:
            raise ValueError("invalid physical control configuration keys")
        scene = str(Path(config["scene"]).resolve())
        if not scene.isascii() or _asset_digest.__wrapped__(scene) != config["asset_digest"]:
            raise ValueError("physical control asset identity mismatch")
        seed = config["seed"]
        if type(seed) is not int:
            raise ValueError("seed must be an integer")
        radial = config["spawn_radial"]
        angle = config["spawn_angle_deg"]
        if not (isinstance(radial, list) and len(radial) == 2 and isinstance(angle, list) and len(angle) == 2):
            raise ValueError("invalid spawn bounds")

        self._events: list[dict[str, Any]] = []

        def _capture_event(ev: GuardEvent) -> None:
            self._events.append({"kind": ev.kind, "reason": ev.reason, "detail": dict(ev.detail)})

        limits_cfg = config["limits"]
        guard = None
        if limits_cfg is not None:
            limits = GuardLimits(
                joint_lower=tuple(float(v) for v in limits_cfg["joint_lower"]),
                joint_upper=tuple(float(v) for v in limits_cfg["joint_upper"]),
                max_delta_per_write=float(limits_cfg["max_delta_per_write"]),
                ee_fence_min=tuple(float(v) for v in limits_cfg["ee_fence_min"]),
                ee_fence_max=tuple(float(v) for v in limits_cfg["ee_fence_max"]),
                allowed_sources=frozenset(str(s) for s in limits_cfg["allowed_sources"]),
            )
            guard = Guard(limits, on_event=_capture_event)

        # 物理子进程内禁用内嵌渲染器，确保物理步进与离屏渲染保持分离。
        # Disable embedded renderers inside the physics worker to keep rendering isolated (D026/D034).
        self.sim = TabletopSim(
            guard=guard,
            seed=seed,
            on_guard_event=_capture_event,
            spawn_radial=(float(radial[0]), float(radial[1])),
            spawn_angle_deg=(float(angle[0]), float(angle[1])),
            isolated_render=False,
        )
        self._events.clear()

    def _guard_state(self) -> dict[str, Any]:
        g = self.sim.guard
        events = list(self._events)
        self._events.clear()
        return {
            "guard_halted": bool(g.halted),
            "guard_halt_reason": str(g.halt_reason),
            "guard_generation": int(g.generation),
            "guard_events": events,
        }

    def metadata(self) -> dict[str, Any]:
        lim = self.sim.guard.limits
        return {
            "pid": os.getpid(),
            "spec": _ser_spec(self.sim),
            "timestep": float(self.sim.timestep),
            "home_qpos": [float(v) for v in self.sim.home_qpos()],
            "stop_profile": self.sim.stop_controller.profile.model_dump(mode="json"),
            "state_epoch": self.sim.state_epoch,
            "nq": int(self.sim.model.nq),
            "nv": int(self.sim.model.nv),
            "limits": {
                "joint_lower": list(lim.joint_lower),
                "joint_upper": list(lim.joint_upper),
                "max_delta_per_write": float(lim.max_delta_per_write),
                "ee_fence_min": list(lim.ee_fence_min),
                "ee_fence_max": list(lim.ee_fence_max),
                "allowed_sources": sorted(lim.allowed_sources),
            },
            "initial_obs": _ser_obs(self.sim._read_state()),
            **self._guard_state(),
        }

    def execute(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if operation not in ALLOWED_CONTROL_OPERATIONS:
            raise ValueError(f"unsupported control operation {operation!r}")
        sim = self.sim
        self._events.clear()

        if operation == "read":
            if arguments:
                raise ValueError("read takes no arguments")
            return {"obs": _ser_obs(sim._read_state()), "state_epoch": sim.state_epoch, **self._guard_state()}

        if operation == "read_control_feedback":
            if arguments:
                raise ValueError("read_control_feedback takes no arguments")
            obs = sim._read_state()
            obs.objects = {}
            obs.extras = {}
            return {"obs": _ser_obs(obs), "state_epoch": sim.state_epoch, **self._guard_state()}

        if operation == "write":
            if set(arguments) != {"joint_targets", "gripper", "source"}:
                raise ValueError("invalid write arguments")
            targets = arguments["joint_targets"]
            gripper = arguments["gripper"]
            source = arguments["source"]
            if not isinstance(targets, list) or not isinstance(source, str):
                raise ValueError("invalid write payload types")
            if gripper is not None and type(gripper) not in (int, float):
                raise ValueError("invalid gripper target type")
            cmd = ActionCommand(
                joint_targets=tuple(float(v) for v in targets),
                gripper=None if gripper is None else float(gripper),
                source=source,
            )
            res = sim.write(cmd)
            applied_cmd = None
            if res.applied and sim._last_cmd is not None:
                applied_cmd = {
                    "joint_targets": list(sim._last_cmd.joint_targets),
                    "gripper": sim._last_cmd.gripper,
                    "source": sim._last_cmd.source,
                }
            return {
                "applied": bool(res.applied),
                "reason": str(res.reason),
                "clamped": bool(res.clamped),
                "applied_cmd": applied_cmd,
                "state_epoch": sim.state_epoch,
                **self._guard_state(),
            }

        if operation == "step":
            if set(arguments) != {"n", "return_obs"}:
                raise ValueError("invalid step arguments")
            n, return_obs = arguments["n"], arguments["return_obs"]
            if type(n) is not int or not 1 <= n <= 10000 or type(return_obs) is not bool:
                raise ValueError("invalid step count or flag")
            sim.step(n)
            return {
                "state_epoch": sim.state_epoch,
                "obs": _ser_obs(sim._read_state()) if return_obs else None,
                **self._guard_state(),
            }

        if operation == "reset":
            if set(arguments) != {"randomize"} or type(arguments["randomize"]) is not bool:
                raise ValueError("invalid reset arguments")
            try:
                obs = sim.reset(randomize=arguments["randomize"])
            except RuntimeError as exc:
                return {"status": "rejected", "error_type": "RuntimeError", "reason": str(exc)}
            return {"status": "ok", "obs": _ser_obs(obs), "state_epoch": sim.state_epoch, **self._guard_state()}

        if operation == "solve_reach":
            if set(arguments) != {"target_pos", "q_init", "approach_down"}:
                raise ValueError("invalid solve_reach arguments")
            target = arguments["target_pos"]
            q_init = arguments["q_init"]
            approach_down = arguments["approach_down"]
            if (
                not isinstance(target, list)
                or len(target) != 3
                or type(approach_down) is not bool
                or (q_init is not None and not isinstance(q_init, list))
            ):
                raise ValueError("invalid solve_reach payload")
            res = sim.solve_reach(
                np.asarray(target, dtype=float),
                q_init=None if q_init is None else np.asarray(q_init, dtype=float),
                approach_down=approach_down,
            )
            return {
                "qpos": [float(v) for v in res.qpos],
                "pos_err": float(res.pos_err),
                "axis_err": float(res.axis_err),
                "converged": bool(res.converged),
                "iters": int(res.iters),
            }

        if operation == "render_snapshot":
            if set(arguments) != {"camera"} or not isinstance(arguments["camera"], str):
                raise ValueError("invalid render_snapshot arguments")
            with sim._state_lock:
                if mujoco.mj_name2id(sim.model, mujoco.mjtObj.mjOBJ_CAMERA, arguments["camera"]) < 0:
                    return {"status": "rejected", "error_type": "ValueError", "reason": "unknown camera"}
                state = {name: encode_array(getattr(sim.data, name).copy()) for name in DATA_FIELDS}
                state.update({name: encode_array(getattr(sim.data, name).copy()) for name in POSE_FIELDS})
                state.update({name: encode_array(getattr(sim.model, name).copy()) for name in CAMERA_FIELDS})
                state["time"] = float(sim.data.time)
                return {"status": "ok", "epoch": sim.state_epoch, "time": state["time"], "state": state}

        if operation == "camera_model":
            if set(arguments) != {"camera", "width", "height"}:
                raise ValueError("invalid camera_model arguments")
            camera, width, height = arguments["camera"], arguments["width"], arguments["height"]
            if not isinstance(camera, str) or type(width) is not int or type(height) is not int:
                raise ValueError("invalid camera_model types")
            try:
                model = sim.camera_model(camera, width=width, height=height)
            except ValueError as exc:
                return {"status": "rejected", "error_type": "ValueError", "reason": str(exc)}
            return {"status": "ok", "camera": asdict(model)}

        if operation == "estop":
            if set(arguments) != {"reason"} or not isinstance(arguments["reason"], str):
                raise ValueError("invalid estop arguments")
            sim.guard.estop(arguments["reason"])
            return {**self._guard_state()}

        if operation == "guard_reset":
            if arguments:
                raise ValueError("guard_reset takes no arguments")
            sim.guard.reset()
            return {**self._guard_state()}

        if operation == "stop_prepare":
            if set(arguments) != {"request", "remaining_s"}:
                raise ValueError("invalid stop_prepare arguments")
            rem = arguments["remaining_s"]
            if type(rem) not in (int, float) or not math.isfinite(rem) or rem <= 0:
                return {"status": "rejected", "error_type": "StopRejected", "reason": "stop_request_expired"}
            req_dict = dict(arguments["request"])
            # 转换到本子进程单调时钟，父进程会用自身时钟二次复核。
            # Rebase deadline to child monotonic clock; parent independently re-validates on parent clock.
            req_dict["deadline_monotonic"] = time.monotonic() + float(rem)
            try:
                child_req = StopRequest.model_validate_json(json_payload(req_dict))
                evidence = sim.stop_controller.prepare(child_req)
            except (StopRejected, ValueError) as exc:
                return {"status": "rejected", "error_type": "StopRejected", "reason": str(exc)}
            return {
                "status": "ok",
                "evidence": evidence.model_dump(mode="json"),
                "state_epoch": sim.state_epoch,
                "stop_fence": sim._stop_fence,
                **self._guard_state(),
            }

        if operation == "stop_commit":
            if set(arguments) != {"nonce", "scene_epoch"}:
                raise ValueError("invalid stop_commit arguments")
            nonce, scene_epoch = arguments["nonce"], arguments["scene_epoch"]
            with sim._state_lock:
                sc = sim.stop_controller
                if sc._issued is None or sim._stop_fence != nonce:
                    return {"status": "rejected", "error_type": "StopRejected", "reason": "stop_evidence_not_issued_here"}
                if sim.state_epoch != scene_epoch:
                    return {"status": "rejected", "error_type": "StopRejected", "reason": "stop_scene_changed"}
                sim._stop_fence = None
                sc._owner = None
                sc._issued = None
            return {"status": "ok", "state_epoch": sim.state_epoch, **self._guard_state()}

        if operation == "stop_abort":
            if set(arguments) != {"nonce"}:
                raise ValueError("invalid stop_abort arguments")
            return {"status": "ok", "stop_fence": sim._stop_fence, **self._guard_state()}

        raise ValueError(f"unhandled operation {operation!r}")

    def close(self) -> None:
        self.sim.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent-pid", required=True, type=int)
    args = parser.parse_args()
    bind_parent_death(args.parent_pid)
    source, sink = sys.stdin.buffer, sys.stdout.buffer
    backend: PhysicalControlBackend | None = None
    boot: str | None = None
    last_request = 0

    def reply(request: ControlWorkerEnvelope, kind: str, payload: dict[str, Any]) -> None:
        message = ControlWorkerEnvelope(
            boot_id=request.boot_id,
            request_id=request.request_id,
            kind=kind,  # type: ignore[arg-type]
            payload_json=json_payload(payload),
        )
        sink.write(encode_control_frame(message))
        sink.flush()

    try:
        while True:
            try:
                request = read_control_frame(source)
            except EOFError:
                break
            if (
                not request.request_id.isascii()
                or not request.request_id.isdigit()
                or request.request_id.startswith("0")
                or int(request.request_id) <= last_request
                or (boot is not None and request.boot_id != boot)
            ):
                raise ValueError("control worker identity rejected")
            last_request = int(request.request_id)
            try:
                payload = parse_payload(request.payload_json)
                with contextlib.redirect_stdout(sys.stderr):
                    if backend is None and request.kind == "init":
                        backend = PhysicalControlBackend(payload)
                        boot = request.boot_id
                        result, kind = backend.metadata(), "ready"
                    elif backend is not None and request.kind == "call":
                        if set(payload) != {"operation", "arguments"} or not isinstance(payload["arguments"], dict):
                            raise ValueError("invalid control call payload")
                        result, kind = backend.execute(payload["operation"], payload["arguments"]), "ok"
                    elif backend is not None and request.kind == "shutdown":
                        backend.close()
                        backend = None
                        reply(request, "ok", {})
                        break
                    else:
                        raise ValueError("invalid control worker phase")
                reply(request, kind, result)
            except Exception as exc:
                reply(request, "error", {"reason": type(exc).__name__})
                break
    finally:
        if backend is not None:
            with contextlib.redirect_stdout(sys.stderr):
                backend.close()


if __name__ == "__main__":
    main()
