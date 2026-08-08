"""Pinhole camera model: intrinsics + world pose + back-projection (D015).

Lives in the control layer (L1): a camera is hardware. The sim driver builds one
from MuJoCo's camera; the real RGB-D driver (M3) will construct the same model
from factory intrinsics + hand-eye calibration — consumers upstream never care.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np


@dataclass(frozen=True)
class CameraModel:
    """MuJoCo camera-frame convention: -Z is the viewing direction, +X right, +Y up."""

    width: int
    height: int
    fx: float
    fy: float
    cx: float
    cy: float
    pos: tuple[float, float, float]
    rot: tuple[float, ...]  # row-major 3x3, camera -> world

    @classmethod
    def from_mujoco(cls, model: Any, data: Any, camera: str, width: int, height: int) -> "CameraModel":
        import mujoco

        cam_id = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_CAMERA, camera)
        if cam_id < 0:
            raise ValueError(f"camera {camera!r} not found")
        fovy_rad = float(np.deg2rad(model.cam_fovy[cam_id]))
        fy = (height / 2.0) / np.tan(fovy_rad / 2.0)
        return cls(
            width=width, height=height,
            fx=float(fy), fy=float(fy),  # MuJoCo pixels are square; fovy defines both
            cx=(width - 1) / 2.0, cy=(height - 1) / 2.0,
            pos=tuple(float(v) for v in data.cam_xpos[cam_id]),
            rot=tuple(float(v) for v in np.asarray(data.cam_xmat[cam_id]).reshape(-1)),
        )

    def backproject(self, u: float, v: float, depth_m: float) -> np.ndarray:
        """Pixel + depth -> world point. Depth is distance along the view axis (-Z)."""
        x_cam = (u - self.cx) / self.fx * depth_m
        y_cam = -(v - self.cy) / self.fy * depth_m  # image v grows down, camera +Y is up
        p_cam = np.array([x_cam, y_cam, -depth_m])
        rot = np.asarray(self.rot).reshape(3, 3)
        return np.asarray(self.pos) + rot @ p_cam

    def ray_dir(self, u: float, v: float) -> np.ndarray:
        """Unit world-frame direction from the camera through pixel (u, v)."""
        p = self.backproject(u, v, 1.0) - np.asarray(self.pos)
        return p / np.linalg.norm(p)

    def backproject_points(self, us: np.ndarray, vs: np.ndarray, depths: np.ndarray) -> np.ndarray:
        """Vectorized backproject: pixel arrays + depths -> (N, 3) world points."""
        x_cam = (np.asarray(us, dtype=np.float64) - self.cx) / self.fx * depths
        y_cam = -(np.asarray(vs, dtype=np.float64) - self.cy) / self.fy * depths
        p_cam = np.stack([x_cam, y_cam, -np.asarray(depths, dtype=np.float64)], axis=1)
        rot = np.asarray(self.rot).reshape(3, 3)
        return p_cam @ rot.T + np.asarray(self.pos)
