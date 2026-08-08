"""Perception pipeline v1: detections + depth -> world-frame object poses (D015).

The pipeline turns a PerceptionProvider's 2D detections into the same
``{name: Pose}`` dict that sim ground truth fills today — the D014
environment_state replacement seam. ``PerceivedSim`` wraps an Embodiment so the
AGENT sees perceived objects while proprioception stays real and judges keep
``read_truth()`` (perception feeds the agent, never the referee).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from embodied.cognition.world_state import GRASP_HOLD_RADIUS
from embodied.control.camera import CameraModel
from embodied.control.hal import Observation, Pose
from embodied.providers.perception import BasePerceptionProvider, Detection

_IDENTITY_QUAT = (1.0, 0.0, 0.0, 0.0)  # v1: single view -> no orientation estimate

# Kinematic-attachment belief thresholds (against normalized gripper opening).
# Scripted skills close to 0.0 for a grasp and open to 0.8 to release; the bands sit
# safely inside those envelopes (approach opening is 0.6 — never mistaken for a hold).
ATTACH_BELOW_OPENING = 0.45
DETACH_ABOVE_OPENING = 0.7

# Quality gates against the object's expected pixel area (one face at the measured
# depth). Lower bound: partial occlusion — e.g. the gripper hovering over the cube —
# yields a biased crescent (measured 0.06x expected vs 1.15x clean). Upper bound:
# a detection far larger than the object can appear (clean views measure 1.15-1.6x)
# is some OTHER structure confidently mislabeled (open-vocab detectors do this).
# Rejected detections let the next camera or the registry's last good pose win.
MIN_VISIBLE_FRACTION = 0.35
MAX_VISIBLE_RATIO = 4.0

# Surface-centroid -> object-center push, in units of the object size, along the view
# ray. 0.5 is exact for a single flat face seen head-on; oblique views exposing two
# faces have their centroid already closer to the center and want less. Values below
# are a per-camera sensor calibration against sim truth (mean error ~2 mm each).
DEFAULT_PUSH_FRAC = 0.5
CAMERA_PUSH_FRAC = {"top": 0.45, "side": 0.3}


@dataclass(frozen=True)
class ObjectSpec:
    """What the pipeline knows about one queryable object."""

    prompt: str  # provider query ("color:red" / "a small red cube")
    approx_size_m: float = 0.03  # depth hits the visible surface; push half a size deeper


# Scene knowledge for the tabletop world. Scene-manifest driven later; v1 keeps it here.
TABLETOP_OBJECTS: dict[str, ObjectSpec] = {
    "obj_cube": ObjectSpec(prompt="color:red", approx_size_m=0.03),
}

DINO_TABLETOP_OBJECTS: dict[str, ObjectSpec] = {
    "obj_cube": ObjectSpec(prompt="a small red cube", approx_size_m=0.03),
}


@dataclass
class PerceptionPipeline:
    provider: BasePerceptionProvider
    objects: dict[str, ObjectSpec] = field(default_factory=lambda: dict(TABLETOP_OBJECTS))

    def locate(
        self, rgb: np.ndarray, depth: np.ndarray, cam: CameraModel, *, push_frac: float = DEFAULT_PUSH_FRAC
    ) -> dict[str, Pose]:
        """Detect every configured object and lift it to a world-frame Pose.

        Position = centroid of the per-pixel back-projected VISIBLE SURFACE (depth
        inliers around the median — arm pixels bleeding into a color mask sit at a
        different depth and are dropped), pushed ``push_frac * size`` along the view
        ray toward the object interior. Undetected objects are simply absent —
        downstream already treats objects as a sparse dict.
        """
        prompts = [spec.prompt for spec in self.objects.values()]
        detections = {d.label: d for d in self.provider.detect(np.asarray(rgb), prompts)}
        out: dict[str, Pose] = {}
        for name, spec in self.objects.items():
            det = detections.get(spec.prompt)
            if det is None:
                continue
            point = self._lift(det, depth, cam, spec, push_frac)
            if point is not None:
                out[name] = Pose(pos=(float(point[0]), float(point[1]), float(point[2])), quat=_IDENTITY_QUAT)
        return out

    @staticmethod
    def _lift(
        det: Detection, depth: np.ndarray, cam: CameraModel, spec: ObjectSpec, push_frac: float
    ) -> np.ndarray | None:
        x0, y0, x1, y1 = det.bbox
        if det.mask is not None:
            ys, xs = np.nonzero(det.mask)
        else:
            # Central half of the bbox: robust to box slop without needing a mask.
            wq, hq = max(1, (x1 - x0) // 4), max(1, (y1 - y0) // 4)
            ys, xs = np.mgrid[y0 + hq : max(y0 + hq + 1, y1 - hq), x0 + wq : max(x0 + wq + 1, x1 - wq)]
            ys, xs = ys.ravel(), xs.ravel()
        ys = np.clip(ys, 0, depth.shape[0] - 1)
        xs = np.clip(xs, 0, depth.shape[1] - 1)
        d = np.asarray(depth[ys, xs], dtype=np.float64)
        valid = np.isfinite(d) & (d > 0)
        if not valid.any():
            return None
        ys, xs, d = ys[valid], xs[valid], d[valid]
        # Depth-consistent inliers only: color bleed from other geometry (the arm's
        # printed parts) sits at a different depth and would drag the centroid.
        inlier = np.abs(d - float(np.median(d))) <= spec.approx_size_m
        ys, xs, d = ys[inlier], xs[inlier], d[inlier]
        if d.size == 0:
            return None
        depth_med = float(np.median(d))
        expected_px = (spec.approx_size_m * cam.fx / depth_med) ** 2
        # Gates compare the DETECTION's raw extent (mask pixels, or full bbox area) to
        # the expected one-face area; the pose estimate itself uses only inlier pixels.
        visible = float(det.mask.sum()) if det.mask is not None else float((x1 - x0) * (y1 - y0))
        if visible < MIN_VISIBLE_FRACTION * expected_px:
            return None  # mostly occluded: a biased crescent estimate is worse than none
        if visible > MAX_VISIBLE_RATIO * expected_px:
            return None  # far too large for the object at this depth: mislabeled structure
        surface = cam.backproject_points(xs, ys, d).mean(axis=0)
        ray = surface - np.asarray(cam.pos)
        ray = ray / np.linalg.norm(ray)
        return surface + ray * (push_frac * spec.approx_size_m)


class PerceivedSim:
    """Embodiment wrapper: the agent's eyes are the perception pipeline + object registry.

    Only ``Observation.objects`` is replaced — joint state, gripper and ee pose
    remain encoder truth (perception must never corrupt proprioception or the
    guard's inputs). Perception is throttled by sim time (``min_interval_s``);
    ``read_truth()`` exposes the unwrapped observation for eval judges.

    Occlusion strategy (the arm shades parts of the workspace from any single
    camera, and the gripper itself hides the target mid-grasp):
    - multiple cameras, tried in order until every configured object is seen;
    - an OBJECT REGISTRY with persistence — a momentarily-undetected object keeps
      its last known pose instead of vanishing (object permanence; the registry
      clears on reset). Staleness marking is a v2 concern;
    - a KINEMATIC-ATTACHMENT belief: when the gripper closes at an object's last
      known pose, the unseen object is believed to move WITH the hand (updated from
      encoder proprioception, no ground truth involved) until the gripper opens or
      a fresh sighting contradicts the belief (slipped grasp -> detach, honest fail).
    """

    def __init__(
        self,
        sim: Any,
        pipeline: PerceptionPipeline,
        *,
        cameras: tuple[str, ...] = ("top", "side"),
        width: int = 640,
        height: int = 480,
        min_interval_s: float = 0.25,
    ) -> None:
        self._sim = sim
        self._pipeline = pipeline
        self._cameras = tuple(cameras)
        self._size = (width, height)
        self._min_interval = min_interval_s
        self._cache_t = -1e9
        self._registry: dict[str, Pose] = {}
        self._attached: tuple[str, np.ndarray] | None = None  # (object, ee->object offset)
        # GL contexts are thread-bound: skills run in worker threads (asyncio.to_thread)
        # while other reads happen on the loop thread. Funnel EVERY render through one
        # dedicated thread so the renderer's context lives exactly there — rendering
        # from whatever thread happens to call read() returns stale/garbage frames
        # (observed: WGL "failed to make context current", camera/image mismatch).
        self._render_thread: ThreadPoolExecutor | None = None

    # -- the perception seam ------------------------------------------------------

    def read(self) -> Observation:
        obs = self._sim.read()
        obs.objects = dict(self._perceive(obs))
        return obs

    def read_truth(self) -> Observation:
        return self._sim.read()

    def reset(self, **kwargs: Any) -> Observation:
        obs = self._sim.reset(**kwargs)
        self._cache_t = -1e9
        self._registry = {}  # scene changed: no stale carryover across episodes
        self._attached = None
        obs.objects = dict(self._perceive(obs))
        return obs

    def close(self) -> None:
        """Tear renderers down ON the thread that owns their GL contexts.

        Cross-thread GLFW destruction at interpreter shutdown segfaults; anything
        that wraps a sim should close() when done (CLI/eval/tests do)."""
        if self._render_thread is None:
            return

        def _cleanup() -> None:
            import threading

            tid = threading.get_ident()  # running ON the render thread
            renderers = self._sim.__dict__.get("_renderers", {})
            for key in [k for k in renderers if k[0] == tid]:
                try:
                    renderers.pop(key).close()
                except Exception:
                    pass

        try:
            self._render_thread.submit(_cleanup).result(timeout=5)
        except Exception:
            pass
        self._render_thread.shutdown(wait=True)
        self._render_thread = None

    def _render_rgbd(self, camera: str, width: int, height: int):
        if self._render_thread is None:
            self._render_thread = ThreadPoolExecutor(max_workers=1, thread_name_prefix="perception-render")
        return self._render_thread.submit(self._sim.render_rgbd, camera, width, height).result()

    def _perceive(self, obs: Observation) -> dict[str, Pose]:
        if obs.t - self._cache_t >= self._min_interval:
            width, height = self._size
            wanted = set(self._pipeline.objects)
            seen: dict[str, Pose] = {}
            for camera in self._cameras:
                rgb, depth = self._render_rgbd(camera, width, height)
                cam = self._sim.camera_model(camera, width=width, height=height)
                push = CAMERA_PUSH_FRAC.get(camera, DEFAULT_PUSH_FRAC)
                for name, pose in self._pipeline.locate(rgb, depth, cam, push_frac=push).items():
                    seen.setdefault(name, pose)  # earlier cameras win (listed by trust)
                if wanted <= set(seen):
                    break  # all found: skip the remaining renders
            self._registry.update(seen)  # persistence: a missed object keeps its last pose
            self._update_attachment(obs, seen)
            self._cache_t = obs.t
        return self._registry

    def _update_attachment(self, obs: Observation, seen: dict[str, Pose]) -> None:
        """Belief update from proprioception: closed hand at an object => it moves with us."""
        ee = np.asarray(obs.ee_pose.pos)
        grip = float(obs.gripper_opening)
        if self._attached is None:
            if grip < ATTACH_BELOW_OPENING:
                for name, pose in self._registry.items():
                    if float(np.linalg.norm(np.asarray(pose.pos) - ee)) < GRASP_HOLD_RADIUS:
                        self._attached = (name, np.asarray(pose.pos) - ee)
                        break
        else:
            name, _off = self._attached
            if grip > DETACH_ABOVE_OPENING:
                self._attached = None  # released: object stays where it was last believed
            elif name in seen:
                d = float(np.linalg.norm(np.asarray(seen[name].pos) - ee))
                if d < GRASP_HOLD_RADIUS * 1.5:
                    self._attached = (name, np.asarray(seen[name].pos) - ee)  # refresh offset
                else:
                    self._attached = None  # sighted far from the hand: we are NOT holding it
        if self._attached is not None:
            name, off = self._attached
            if name not in seen:  # unseen this tick: carried by the hand per the belief
                p = ee + off
                self._registry[name] = Pose(pos=(float(p[0]), float(p[1]), float(p[2])), quat=_IDENTITY_QUAT)

    # -- everything else passes through -------------------------------------------

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):  # never proxy privates (and avoid __init__ recursion)
            raise AttributeError(name)
        return getattr(self._sim, name)
