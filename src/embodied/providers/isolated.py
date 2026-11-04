"""Data-only adapters for isolated detection and policy inference. 隔离检测与策略推理的纯数据适配器。"""

from __future__ import annotations

import hashlib
import math
from dataclasses import asdict
from pathlib import Path

import numpy as np

from embodied.contracts.compute import decode_array, encode_array
from embodied.providers.perception import BasePerceptionProvider, ColorBlobProvider, Detection, GroundingDinoProvider
from embodied.providers.policy import BasePolicyProvider, OnnxPolicy, PolicyMeta
from embodied.runtime.compute import ComputeProcess


def _image(rgb):
    rgb = np.asarray(rgb)
    if rgb.dtype != np.uint8 or rgb.ndim != 3 or rgb.shape[2] != 3 or not all(0 < d <= 1024 for d in rgb.shape[:2]):
        raise ValueError("invalid detection image")
    return rgb


def _prompts(prompts):
    if (not isinstance(prompts, list) or not 0 < len(prompts) <= 8 or len(set(prompts)) != len(prompts)
            or any(not isinstance(p, str) or not 0 < len(p) <= 512 for p in prompts)):
        raise ValueError("invalid detection prompts")
    return prompts


class ProcessPerception(BasePerceptionProvider):
    def __init__(self, kind: str, *, model_id=None, threshold=0.4):
        config = {"backend": kind}
        if kind == "dino":
            config.update(model_id=model_id, threshold=threshold)
        self.worker = ComputeProcess(config)
        self.worker.start()

    def detect(self, rgb, prompts):
        rgb, prompts = _image(rgb), _prompts(prompts)
        h, w = rgb.shape[:2]

        def validate(reply):
            if set(reply) != {"detections"} or not isinstance(reply["detections"], list) or len(reply["detections"]) > len(prompts):
                raise ValueError("invalid detections")
            result, seen = [], set()
            for item in reply["detections"]:
                if not isinstance(item, dict) or set(item) != {"label", "score", "bbox", "mask"}:
                    raise ValueError("invalid detection fields")
                label, score, box = item["label"], item["score"], item["bbox"]
                if (label not in prompts or label in seen or type(score) not in (float, int)
                        or not math.isfinite(score) or not 0 <= score <= 1 or not isinstance(box, list)
                        or len(box) != 4 or any(type(n) is not int for n in box)
                        or not 0 <= box[0] < box[2] <= w or not 0 <= box[1] < box[3] <= h):
                    raise ValueError("invalid detection values")
                seen.add(label)
                mask = None if item["mask"] is None else decode_array(item["mask"], dtype="bool", shape=(h, w))
                result.append(Detection(label=label, score=float(score), bbox=tuple(box), mask=mask))
            return result

        return self.worker.call("detect", {"rgb": encode_array(rgb), "prompts": prompts}, validate=validate)

    def close(self):
        self.worker.close()

    def restart(self):
        self.worker.restart()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class ProcessPolicy(BasePolicyProvider):
    def __init__(self, path):
        path = Path(path).resolve()
        self.worker = ComputeProcess({"backend": "onnx", "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        try:
            data = dict(self.worker.start()["meta"])
            data["state_names"], data["env_names"] = tuple(data["state_names"]), tuple(data["env_names"])
            self._meta = PolicyMeta(**data)
            m = self._meta
            if (any(type(n) is not int or not 0 < n <= 4096 for n in (m.chunk_size, m.state_dim, m.action_dim))
                    or type(m.env_dim) is not int or not 0 <= m.env_dim <= 4096
                    or not math.isfinite(m.control_hz) or m.control_hz <= 0):
                raise ValueError("invalid policy metadata")
        except BaseException:
            self.worker.close()
            raise

    @property
    def meta(self):
        return self._meta

    def act(self, state, env):
        state, env = np.asarray(state, dtype=np.float32), np.asarray(env, dtype=np.float32)
        if state.shape != (self.meta.state_dim,) or env.shape != (self.meta.env_dim,):
            raise ValueError("policy input shape mismatch")
        def validate(reply):
            if set(reply) != {"chunk"}:
                raise ValueError("invalid policy response fields")
            return decode_array(reply["chunk"], dtype="float32", shape=(self.meta.chunk_size, self.meta.action_dim))
        return self.worker.call("act", {"state": encode_array(state), "env": encode_array(env)}, validate=validate)

    def reset(self):
        self.worker.call("reset", {})

    def close(self):
        self.worker.close()

    def restart(self):
        self.worker.restart()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class _Backend:
    def __init__(self, provider):
        self.provider = provider

    def metadata(self):
        return {"meta": asdict(self.provider.meta)} if isinstance(self.provider, BasePolicyProvider) else {}

    def execute(self, operation, arguments):
        if isinstance(self.provider, BasePolicyProvider):
            if operation == "reset" and not arguments:
                self.provider.reset()
                return {}
            if operation != "act" or set(arguments) != {"state", "env"}:
                raise ValueError("invalid policy operation")
            m = self.provider.meta
            chunk = self.provider.act(decode_array(arguments["state"], dtype="float32", shape=(m.state_dim,)),
                                      decode_array(arguments["env"], dtype="float32", shape=(m.env_dim,)))
            return {"chunk": encode_array(chunk)}
        if operation != "detect" or set(arguments) != {"rgb", "prompts"}:
            raise ValueError("invalid perception operation")
        rgb = _image(decode_array(arguments["rgb"], dtype="uint8"))
        detections = self.provider.detect(rgb, _prompts(arguments["prompts"]))
        return {"detections": [{"label": d.label, "score": d.score, "bbox": list(d.bbox),
                                "mask": None if d.mask is None else encode_array(d.mask)} for d in detections]}

    def close(self):
        pass


def create_backend(config):
    kind = config.get("backend")
    if kind == "color" and set(config) == {"backend"}:
        return _Backend(ColorBlobProvider())
    if kind == "dino" and set(config) == {"backend", "model_id", "threshold"}:
        provider = GroundingDinoProvider(config["model_id"], config["threshold"])
        provider._load()
        return _Backend(provider)
    if kind == "onnx" and set(config) == {"backend", "path", "sha256"}:
        if hashlib.sha256(Path(config["path"]).read_bytes()).hexdigest() != config["sha256"]:
            raise ValueError("policy artifact changed")
        return _Backend(OnnxPolicy(config["path"]))
    raise ValueError("unsupported compute backend")
