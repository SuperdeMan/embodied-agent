"""MuJoCo tabletop sim driver: the first Embodiment implementation.

Ground-truth object poses flow into Observation.objects until perception lands (M2).
Every write passes through the Safety Guard — skills cannot reach data.ctrl directly,
which is exactly the seam the real-hardware driver (M3) will preserve.
"""

from __future__ import annotations

import math
import threading
from typing import Any, Callable

import mujoco
import numpy as np

from embodied.control import kinematics
from embodied.control.hal import (
    ActionCommand,
    EmbodimentSpec,
    JointSpec,
    Observation,
    Pose,
    WriteResult,
)
from embodied.control.simassets import stage_tabletop
from embodied.safety.guard import Guard, GuardEvent, GuardLimits

ARM_JOINTS = ("Rotation", "Pitch", "Elbow", "Wrist_Pitch", "Wrist_Roll")
JAW_JOINT = "Jaw"
GRASP_SITE = "grasp"
OBJECT_PREFIX = "obj_"
REGION_SUFFIX = "_region"

# Jaw qpos ↔ normalized opening (calibrated against the Menagerie model's pad geometry).
JAW_CLOSED_Q = 0.0
JAW_OPEN_Q = 1.2

StepHook = Callable[[Observation, ActionCommand | None], None]


class TabletopSim:
    """Implements the Embodiment protocol plus reach-solving for scripted skills."""

    def __init__(
        self,
        *,
        guard: Guard | None = None,
        seed: int = 0,
        on_step: StepHook | None = None,
        on_guard_event: Callable[[GuardEvent], None] | None = None,
        spawn_radial: tuple[float, float] = (0.18, 0.27),
        spawn_angle_deg: tuple[float, float] = (230.0, 310.0),
    ) -> None:
        scene = stage_tabletop()
        self.model = mujoco.MjModel.from_xml_path(str(scene))
        self.data = mujoco.MjData(self.model)
        self._rng = np.random.default_rng(seed)
        self._on_step = on_step
        self._last_cmd: ActionCommand | None = None
        # The DEFAULTS are part of eval task semantics (tabletop_pick_place@v1 documents
        # them) — never change them without a new task version. Overrides exist for
        # targeted data collection (e.g. oversampling a failure zone).
        self._spawn_radial = (float(spawn_radial[0]), float(spawn_radial[1]))
        self._spawn_angle_deg = (float(spawn_angle_deg[0]), float(spawn_angle_deg[1]))

        m = self.model
        self._jids = [self._name2id(mujoco.mjtObj.mjOBJ_JOINT, n) for n in ARM_JOINTS]
        self._qpos_idx = np.array([m.jnt_qposadr[j] for j in self._jids])
        self._dof_idx = np.array([m.jnt_dofadr[j] for j in self._jids])
        self._lower = m.jnt_range[self._jids, 0].copy()
        self._upper = m.jnt_range[self._jids, 1].copy()
        jaw_jid = self._name2id(mujoco.mjtObj.mjOBJ_JOINT, JAW_JOINT)
        self._jaw_qpos_adr = int(m.jnt_qposadr[jaw_jid])
        self._act_ids = np.array([self._name2id(mujoco.mjtObj.mjOBJ_ACTUATOR, n) for n in ARM_JOINTS])
        self._jaw_act = self._name2id(mujoco.mjtObj.mjOBJ_ACTUATOR, JAW_JOINT)
        self._site_id = self._name2id(mujoco.mjtObj.mjOBJ_SITE, GRASP_SITE)
        self._home_key = self._name2id(mujoco.mjtObj.mjOBJ_KEY, "home")

        self._objects: dict[str, int] = {}
        self._object_qpos_adr: dict[str, int] = {}
        self._object_default: dict[str, np.ndarray] = {}
        mujoco.mj_resetData(m, self.data)  # qpos0 baseline, used to capture object spawn poses
        for b in range(m.nbody):
            name = mujoco.mj_id2name(m, mujoco.mjtObj.mjOBJ_BODY, b) or ""
            if name.startswith(OBJECT_PREFIX):
                self._objects[name] = b
                jadr = m.body_jntadr[b]
                if jadr >= 0 and m.jnt_type[jadr] == mujoco.mjtJoint.mjJNT_FREE:
                    adr = int(m.jnt_qposadr[jadr])
                    self._object_qpos_adr[name] = adr
                    default = self.data.qpos[adr : adr + 7].copy()
                    if np.linalg.norm(default[:3]) < 1e-9:  # qpos0 lost the XML pose; fall back to body frame
                        default = np.concatenate([m.body_pos[b], m.body_quat[b]])
                    self._object_default[name] = default
        self._region_sites: dict[str, int] = {}
        for s in range(m.nsite):
            name = mujoco.mj_id2name(m, mujoco.mjtObj.mjOBJ_SITE, s) or ""
            if name.endswith(REGION_SUFFIX):
                self._region_sites[name] = s

        self.guard = guard or Guard(
            GuardLimits(joint_lower=tuple(self._lower), joint_upper=tuple(self._upper)),
            on_event=on_guard_event,
        )
        self.reset()

    # -- Embodiment protocol ---------------------------------------------------

    def spec(self) -> EmbodimentSpec:
        joints = tuple(
            JointSpec(name=n, lower=float(lo), upper=float(hi))
            for n, lo, hi in zip(ARM_JOINTS, self._lower, self._upper)
        )
        jaw = JointSpec(name=JAW_JOINT, lower=JAW_CLOSED_Q, upper=JAW_OPEN_Q)
        return EmbodimentSpec(embodiment_id="sim.mujoco.so_arm100", joints=joints, gripper_joint=jaw)

    def read(self) -> Observation:
        d = self.data
        qpos = tuple(float(v) for v in d.qpos[self._qpos_idx])
        qvel = tuple(float(v) for v in d.qvel[self._dof_idx])
        ee = self._site_pose()
        jaw_q = float(d.qpos[self._jaw_qpos_adr])
        opening = (jaw_q - JAW_CLOSED_Q) / (JAW_OPEN_Q - JAW_CLOSED_Q)
        objects = {
            name: Pose(pos=tuple(map(float, d.xpos[b])), quat=tuple(map(float, d.xquat[b])))
            for name, b in self._objects.items()
        }
        return Observation(
            t=float(d.time),
            qpos=qpos,
            qvel=qvel,
            gripper_opening=float(min(max(opening, 0.0), 1.0)),
            ee_pose=ee,
            objects=objects,
            extras={"regions": self.regions()},
        )

    def write(self, cmd: ActionCommand) -> WriteResult:
        applied, result = self.guard.gate(cmd, self.read())
        if applied is None:
            return result
        self.data.ctrl[self._act_ids] = np.asarray(applied.joint_targets)
        if applied.gripper is not None:
            self.data.ctrl[self._jaw_act] = JAW_CLOSED_Q + applied.gripper * (JAW_OPEN_Q - JAW_CLOSED_Q)
        self._last_cmd = applied
        return result

    def step(self, n: int = 1) -> None:
        for _ in range(n):
            mujoco.mj_step(self.model, self.data)
        if self._on_step is not None:
            self._on_step(self.read(), self._last_cmd)

    def reset(self, *, randomize: bool = False) -> Observation:
        # The upstream "home" keyframe predates our scene objects (MuJoCo zero-pads the
        # extra free-joint slots, which would teleport objects into the arm base), so
        # apply it selectively: arm+jaw from the keyframe, objects from their spawn poses.
        m = self.model
        mujoco.mj_resetData(m, self.data)
        key_qpos = m.key_qpos[self._home_key]
        self.data.qpos[self._qpos_idx] = key_qpos[self._qpos_idx]
        self.data.qpos[self._jaw_qpos_adr] = key_qpos[self._jaw_qpos_adr]
        self.data.ctrl[:] = m.key_ctrl[self._home_key]
        for name, adr in self._object_qpos_adr.items():
            self.data.qpos[adr : adr + 7] = self._object_default[name]
        if randomize:
            self._randomize_objects()
        self.guard.reset()
        self._last_cmd = None
        mujoco.mj_forward(m, self.data)
        self.step(100)  # let free objects settle under gravity while servos hold home
        return self.read()

    # -- extras used by scripted skills ---------------------------------------

    @property
    def timestep(self) -> float:
        return float(self.model.opt.timestep)

    def set_hooks(
        self,
        on_step: StepHook | None = None,
        on_guard_event: Callable[[GuardEvent], None] | None = None,
    ) -> None:
        """Attach/detach per-episode listeners (recorder wiring)."""
        self._on_step = on_step
        self.guard.set_listener(on_guard_event)

    def arm_qpos(self) -> np.ndarray:
        return self.data.qpos[self._qpos_idx].copy()

    def home_qpos(self) -> np.ndarray:
        return self.model.key_qpos[self._home_key][self._qpos_idx].copy()

    def solve_reach(
        self, target_pos: Any, *, q_init: np.ndarray | None = None, approach_down: bool = True
    ) -> kinematics.IKResult:
        return kinematics.solve_ik(
            self.model,
            GRASP_SITE,
            np.asarray(target_pos, dtype=float),
            self.arm_qpos() if q_init is None else np.asarray(q_init, dtype=float),
            self._qpos_idx,
            self._dof_idx,
            base_qpos=self.data.qpos.copy(),
            target_axis_world=(0.0, 0.0, -1.0) if approach_down else None,
            joint_lower=self._lower,
            joint_upper=self._upper,
        )

    def object_pos(self, name: str) -> np.ndarray:
        return self.data.xpos[self._objects[name]].copy()

    def regions(self) -> dict[str, dict[str, tuple[float, float, float]]]:
        out: dict[str, dict[str, tuple[float, float, float]]] = {}
        for name, s in self._region_sites.items():
            center = tuple(float(v) for v in self.data.site_xpos[s])
            half = tuple(float(v) for v in self.model.site_size[s])
            out[name] = {"center": center, "half": half}
        return out

    def _renderer_for(self, width: int, height: int, *, depth: bool) -> "mujoco.Renderer":
        """Renderers are cached per (THREAD, size, kind).

        Sized at construction: a cached 320x240 renderer silently serving a 640x480
        request breaks camera geometry downstream. Thread-keyed: GL contexts bind to
        the creating thread — sharing one renderer between the main thread and a
        perception render thread yields stale/garbage frames (WGL make-current
        failures), so each thread owns its instances outright."""
        cache: dict[tuple[int, int, int, bool], mujoco.Renderer] = self.__dict__.setdefault("_renderers", {})
        key = (threading.get_ident(), width, height, depth)
        r = cache.get(key)
        if r is None:
            r = mujoco.Renderer(self.model, height=height, width=width)
            if depth:
                r.enable_depth_rendering()
            cache[key] = r
        return r

    def render(self, camera: str = "side", width: int = 640, height: int = 480) -> np.ndarray:
        r = self._renderer_for(width, height, depth=False)
        r.update_scene(self.data, camera=camera)
        return r.render()

    def render_rgbd(self, camera: str = "side", width: int = 640, height: int = 480):
        """RGB frame + metric depth map (meters along the view axis) — perception input."""
        rgb = self.render(camera, width, height)
        d = self._renderer_for(width, height, depth=True)
        d.update_scene(self.data, camera=camera)
        return rgb, d.render()

    def camera_model(self, camera: str = "side", width: int = 640, height: int = 480):
        from embodied.control.camera import CameraModel

        return CameraModel.from_mujoco(self.model, self.data, camera, width, height)

    # -- internals -------------------------------------------------------------

    def _name2id(self, objtype: int, name: str) -> int:
        oid = mujoco.mj_name2id(self.model, objtype, name)
        if oid < 0:
            raise ValueError(f"{name!r} (type {objtype}) not found in model")
        return oid

    def _site_pose(self) -> Pose:
        pos = self.data.site_xpos[self._site_id]
        quat = np.zeros(4)
        mujoco.mju_mat2Quat(quat, self.data.site_xmat[self._site_id])
        return Pose(pos=tuple(map(float, pos)), quat=tuple(map(float, quat)))

    def _randomize_objects(self) -> None:
        """Uniform placement in the (calibrated, or overridden) reachable sector, away from the bin."""
        for name, adr in self._object_qpos_adr.items():
            for _ in range(64):
                r = self._rng.uniform(*self._spawn_radial)
                theta = self._rng.uniform(
                    math.radians(self._spawn_angle_deg[0]), math.radians(self._spawn_angle_deg[1])
                )
                x, y = r * math.cos(theta), r * math.sin(theta)
                if math.hypot(x - 0.16, y + 0.18) < 0.10:  # keep clear of the bin
                    continue
                self.data.qpos[adr : adr + 7] = [x, y, 0.0155, 1.0, 0.0, 0.0, 0.0]
                break
