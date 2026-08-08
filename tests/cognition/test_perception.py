"""Perception v1 units — hermetic (numpy only, no mujoco/transformers).

Covers the D015 chain piece by piece: HSV conversion, color-blob detection,
pinhole back-projection self-consistency, depth lifting with the half-size ray
correction, and the PerceivedSim seam (agent sees perception, judges see truth,
proprioception never touched).
"""

from __future__ import annotations

import colorsys

import numpy as np

from embodied.cognition.perception import (
    ObjectSpec,
    PerceivedSim,
    PerceptionPipeline,
)
from embodied.control.camera import CameraModel
from embodied.control.hal import Observation, Pose
from embodied.providers.perception import (
    BasePerceptionProvider,
    ColorBlobProvider,
    Detection,
    build_perception_provider,
    rgb_to_hsv,
)


def test_rgb_to_hsv_matches_colorsys():
    rng = np.random.default_rng(0)
    rgb = rng.integers(0, 256, size=(16, 16, 3), dtype=np.uint8)
    hsv = rgb_to_hsv(rgb)
    for y in range(0, 16, 5):
        for x in range(0, 16, 5):
            r, g, b = (float(v) / 255.0 for v in rgb[y, x])
            h, s, v = colorsys.rgb_to_hsv(r, g, b)
            assert np.allclose(hsv[y, x], (h, s, v), atol=1e-5), (y, x)


def test_color_blob_finds_red_square():
    img = np.full((60, 80, 3), 180, dtype=np.uint8)  # grey table
    img[20:30, 45:55] = (200, 20, 25)  # red square
    dets = ColorBlobProvider().detect(img, ["color:red", "color:blue"])
    assert len(dets) == 1 and dets[0].label == "color:red"
    x0, y0, x1, y1 = dets[0].bbox
    assert (x0, y0, x1, y1) == (45, 20, 55, 30)
    assert dets[0].mask is not None and dets[0].mask.sum() == 100


def test_build_perception_provider_factory():
    assert isinstance(build_perception_provider("color"), ColorBlobProvider)
    try:
        build_perception_provider("nope")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def synthetic_cam(f: float = 100.0) -> CameraModel:
    # Camera 1 m above the origin looking straight down: world x -> image right,
    # world y -> image up. rot maps camera frame -> world; -Z_cam must be world -Z.
    # f=100 keeps geometry assertions round; pipeline tests use f=650 (realistic
    # scale so a 20x20 px detection of a 3 cm object passes the visibility gates).
    rot = (1.0, 0.0, 0.0,
           0.0, 1.0, 0.0,
           0.0, 0.0, 1.0)
    return CameraModel(width=100, height=100, fx=f, fy=f, cx=49.5, cy=49.5,
                       pos=(0.0, 0.0, 1.0), rot=rot)


def test_backproject_center_and_offsets():
    cam = synthetic_cam()
    # Center pixel at depth 1.0 -> the world origin.
    assert np.allclose(cam.backproject(49.5, 49.5, 1.0), (0.0, 0.0, 0.0), atol=1e-9)
    # 10 px right of center at depth 1 -> +0.1 world x; 10 px DOWN -> -0.1 world y.
    assert np.allclose(cam.backproject(59.5, 49.5, 1.0), (0.1, 0.0, 0.0), atol=1e-9)
    assert np.allclose(cam.backproject(49.5, 59.5, 1.0), (0.0, -0.1, 0.0), atol=1e-9)
    # Ray through the center points straight down.
    assert np.allclose(cam.ray_dir(49.5, 49.5), (0.0, 0.0, -1.0), atol=1e-9)


class OneBoxProvider(BasePerceptionProvider):
    def __init__(self, bbox):
        self.bbox = bbox

    def detect(self, rgb, prompts):
        return [Detection(label=prompts[0], score=0.9, bbox=self.bbox)]


def test_pipeline_lift_applies_half_size_ray_correction():
    cam = synthetic_cam(f=650.0)
    depth = np.full((100, 100), 0.97, dtype=np.float32)  # top face of a 3 cm cube on the floor
    pipeline = PerceptionPipeline(
        OneBoxProvider((40, 40, 60, 60)),  # centered box -> world (0, 0)
        {"obj_cube": ObjectSpec(prompt="p", approx_size_m=0.03)},
    )
    rgb = np.zeros((100, 100, 3), dtype=np.uint8)
    poses = pipeline.locate(rgb, depth, cam)
    # Surface point (0,0,0.03) pushed 1.5 cm along the down-ray -> cube CENTER (0,0,0.015).
    assert "obj_cube" in poses
    assert np.allclose(poses["obj_cube"].pos, (0.0, 0.0, 0.015), atol=1e-3)


def test_pipeline_skips_invalid_depth():
    cam = synthetic_cam(f=650.0)
    depth = np.zeros((100, 100), dtype=np.float32)  # no valid depth anywhere
    pipeline = PerceptionPipeline(OneBoxProvider((40, 40, 60, 60)), {"obj_cube": ObjectSpec(prompt="p")})
    assert pipeline.locate(np.zeros((100, 100, 3), np.uint8), depth, cam) == {}


def test_pipeline_rejects_oversized_detection():
    """A confident detection far larger than the object can appear at its depth is a
    mislabeled structure (open-vocab failure mode) — dropped by the size gate."""
    cam = synthetic_cam(f=650.0)
    depth = np.full((100, 100), 0.97, dtype=np.float32)
    pipeline = PerceptionPipeline(
        OneBoxProvider((5, 5, 95, 95)),  # 90x90 px >> ~20 px expected for 3 cm at 0.97 m
        {"obj_cube": ObjectSpec(prompt="p", approx_size_m=0.03)},
    )
    assert pipeline.locate(np.zeros((100, 100, 3), np.uint8), depth, cam) == {}


class FakeSimForWrap:
    """Minimal embodiment: truth objects + render_rgbd/camera_model stubs."""

    def __init__(self):
        self.t = 0.0
        self.renders = 0

    def read(self):
        return Observation(
            t=self.t, qpos=(0.1, 0.2), qvel=(0, 0), gripper_opening=0.7,
            ee_pose=Pose(pos=(0, 0, 0.2)),
            objects={"obj_cube": Pose(pos=(9.0, 9.0, 9.0))},  # truth marker
            extras={"regions": {"bin_region": {"center": (0, 0, 0), "half": (1, 1, 1)}}},
        )

    def reset(self, randomize=False):
        return self.read()

    def render_rgbd(self, camera, width, height):
        self.renders += 1
        img = np.zeros((height, width, 3), dtype=np.uint8)
        img[40:60, 40:60] = (220, 15, 15)
        return img, np.full((height, width), 0.97, dtype=np.float32)

    def camera_model(self, camera, width, height):
        return synthetic_cam(f=650.0)

    def step(self, n=1):
        self.t += 0.002 * n

    def spec(self):
        return "spec-sentinel"


def test_perceived_sim_swaps_objects_keeps_proprioception_and_truth():
    inner = FakeSimForWrap()
    wrapped = PerceivedSim(inner, PerceptionPipeline(ColorBlobProvider(), {"obj_cube": ObjectSpec("color:red")}),
                           cameras=("cam0",), width=100, height=100, min_interval_s=0.25)
    obs = wrapped.read()
    assert obs.qpos == (0.1, 0.2) and obs.gripper_opening == 0.7  # proprioception untouched
    assert obs.extras["regions"]  # workspace annotations pass through
    perceived = obs.objects["obj_cube"].pos
    assert perceived != (9.0, 9.0, 9.0) and abs(perceived[2] - 0.015) < 0.005  # the agent's eyes
    assert wrapped.read_truth().objects["obj_cube"].pos == (9.0, 9.0, 9.0)  # the judge's eyes
    assert wrapped.spec() == "spec-sentinel"  # passthrough delegation

    # Throttle: repeated reads within the interval do not re-render.
    wrapped.read()
    wrapped.read()
    assert inner.renders == 1
    inner.step(200)  # 0.4 s of sim time
    wrapped.read()
    assert inner.renders == 2
    # reset invalidates the cache even without time passing
    wrapped.reset(randomize=True)
    assert inner.renders == 3


class BlinkingProvider(BasePerceptionProvider):
    """Detects on the first call, then goes blind — models transient occlusion."""

    def __init__(self):
        self.calls = 0

    def detect(self, rgb, prompts):
        self.calls += 1
        if self.calls > 1:
            return []
        return [Detection(label=prompts[0], score=0.9, bbox=(40, 40, 60, 60))]


def test_registry_keeps_last_pose_through_occlusion_and_clears_on_reset():
    inner = FakeSimForWrap()
    wrapped = PerceivedSim(inner, PerceptionPipeline(BlinkingProvider(), {"obj_cube": ObjectSpec("p")}),
                           cameras=("cam0",), width=100, height=100, min_interval_s=0.25)
    first = wrapped.read().objects
    assert "obj_cube" in first  # seen once
    inner.step(200)
    again = wrapped.read().objects  # provider now blind -> registry persistence
    assert again["obj_cube"].pos == first["obj_cube"].pos
    # reset clears the registry: a never-again-seen object is honestly absent
    wrapped.reset(randomize=True)
    assert "obj_cube" not in wrapped.read().objects
