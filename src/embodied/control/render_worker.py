"""Render a numerical state copy in a private native runtime. 在独立原生运行时渲染数值状态副本。"""

import base64
import io
import math
from pathlib import Path

from embodied.contracts.compute import decode_array, encode_array

DATA_FIELDS = ("qpos", "qvel", "act", "ctrl", "mocap_pos", "mocap_quat")
CAMERA_FIELDS = ("cam_fovy", "cam_pos", "cam_quat")
POSE_FIELDS = ("xpos", "xquat", "xmat", "geom_xpos", "geom_xmat", "site_xpos", "site_xmat",
               "cam_xpos", "cam_xmat", "light_xpos", "light_xdir")


class RenderBackend:
    def __init__(self, config):
        import mujoco

        from embodied.control.stopping import _asset_digest

        if set(config) != {"backend", "scene", "asset_digest"}:
            raise ValueError("invalid render configuration")
        scene = str(Path(config["scene"]).resolve())
        if not scene.isascii() or _asset_digest(scene) != config["asset_digest"]:
            raise ValueError("render asset identity mismatch")
        self.model = mujoco.MjModel.from_xml_path(scene)
        self.data = mujoco.MjData(self.model)
        self.renderers = {}
        mujoco.mj_forward(self.model, self.data)
        renderer = self._renderer(640, 480, False)
        renderer.update_scene(self.data, camera="side")
        renderer.render()

    def metadata(self):
        return {"nq": self.model.nq, "nv": self.model.nv}

    def _renderer(self, width, height, depth):
        import mujoco

        key = width, height, depth
        if key not in self.renderers:
            if len(self.renderers) >= 8:
                raise ValueError("render configuration capacity exceeded")
            renderer = mujoco.Renderer(self.model, width=width, height=height)
            if depth:
                renderer.enable_depth_rendering()
            self.renderers[key] = renderer
        return self.renderers[key]

    def execute(self, operation, arguments):
        import mujoco

        if operation not in {"render", "jpeg"} or set(arguments) != {"state", "camera", "width", "height", "depth", "epoch"}:
            raise ValueError("invalid render request")
        width, height, depth = arguments["width"], arguments["height"], arguments["depth"]
        if (type(width) is not int or type(height) is not int or not 0 < width <= 1024 or not 0 < height <= 1024
                or type(depth) is not bool or not isinstance(arguments["camera"], str)
                or not isinstance(arguments["epoch"], str)):
            raise ValueError("invalid render dimensions")
        state = arguments["state"]
        if set(state) != set(DATA_FIELDS + CAMERA_FIELDS + POSE_FIELDS + ("time",)):
            raise ValueError("invalid render state fields")
        if type(state["time"]) not in (float, int) or not math.isfinite(state["time"]) or state["time"] < 0:
            raise ValueError("invalid render clock")
        for name in DATA_FIELDS:
            target = getattr(self.data, name)
            target[:] = decode_array(state[name], dtype="float64", shape=target.shape)
        for name in CAMERA_FIELDS:
            target = getattr(self.model, name)
            target[:] = decode_array(state[name], dtype="float64", shape=target.shape)
        self.data.time = state["time"]
        mujoco.mj_forward(self.model, self.data)
        # 保留父状态的已计算位姿，避免 step 后重算导致渲染跨一个积分时刻。
        # Preserve sampled derived poses; recomputing after step can shift rendering by one integration instant.
        for name in POSE_FIELDS:
            target = getattr(self.data, name)
            target[:] = decode_array(state[name], dtype="float64", shape=target.shape)
        renderer = self._renderer(width, height, False)
        renderer.update_scene(self.data, camera=arguments["camera"])
        rgb = renderer.render()
        if operation == "jpeg":
            from PIL import Image
            if depth:
                raise ValueError("JPEG request cannot include depth")
            buffer = io.BytesIO()
            Image.fromarray(rgb).save(buffer, format="JPEG", quality=80)
            return {"epoch": arguments["epoch"], "time": self.data.time,
                    "jpeg": base64.b64encode(buffer.getvalue()).decode("ascii")}
        result = {"epoch": arguments["epoch"], "time": self.data.time, "rgb": encode_array(rgb)}
        if depth:
            renderer = self._renderer(width, height, True)
            renderer.update_scene(self.data, camera=arguments["camera"])
            result["depth"] = encode_array(renderer.render())
        return result

    def close(self):
        for renderer in self.renderers.values():
            renderer.close()
        self.renderers.clear()
