"""PerceptionProvider family: 2D open-vocabulary detection (docs/decisions.md D015).

Providers only do image -> detections; 3D pose estimation lives in
``cognition/perception.py`` (depth back-projection), keeping the provider seam
identical for sim renders today and real RGB-D frames in M3.

Two providers ship in v1:
- ``ColorBlobProvider`` — pure-numpy HSV thresholding. Zero deps, offline, fast:
  the CI-testable fallback that proves the perception CHAIN without any weights.
- ``GroundingDinoProvider`` — open-vocabulary detection via transformers
  (``perceive`` dependency group); model id overridable by env.


二维感知提供方只输出检测，三维反投影由认知管线负责；色块提供方离线无额外依赖，DINO 通过可选 transformers 组提供开放词汇检测。
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import numpy as np

DEFAULT_DINO_MODEL = "IDEA-Research/grounding-dino-tiny"

# 颜色提示采用 color 名称约定，色相范围为零到一。
# HSV ranges for the color: prompt convention "color:<name>". Hue in [0, 1).
_COLOR_RANGES: dict[str, tuple[tuple[float, float], float, float]] = {
    # 颜色名映射色相区间及最小饱和度、亮度，支持环绕区间。
    # name -> ((hue_lo, hue_hi) wrapping allowed, min saturation, min value)
    "red": ((0.93, 0.07), 0.45, 0.25),
    "green": ((0.25, 0.45), 0.35, 0.2),
    "blue": ((0.55, 0.72), 0.35, 0.2),
    "yellow": ((0.12, 0.2), 0.35, 0.3),
}


@dataclass
class Detection:
    # 该检测匹配的提示。
    label: str  # the prompt this detection answers
    score: float
    # 像素边框为左上到右下半开区间。
    bbox: tuple[int, int, int, int]  # x0, y0, x1, y1 (pixels, half-open)
    # 可选的高宽布尔掩码。
    mask: np.ndarray | None = field(default=None, repr=False)  # HxW bool, optional


class BasePerceptionProvider(ABC):
    @abstractmethod
    def detect(self, rgb: np.ndarray, prompts: list[str]) -> list[Detection]:
        """``rgb`` HxWx3 uint8; returns at most one best detection per prompt.

        输入为高宽三通道 uint8 图像，每提示最多一个最佳检测。
        """


def rgb_to_hsv(rgb: np.ndarray) -> np.ndarray:
    """Vectorized RGB(uint8) -> HSV(float in [0,1]); mirrors colorsys semantics.

    向量化 RGB 转 HSV，数值语义与 colorsys 一致。
    """
    arr = rgb.astype(np.float32) / 255.0
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    maxc = arr.max(axis=-1)
    minc = arr.min(axis=-1)
    v = maxc
    delta = maxc - minc
    s = np.where(maxc > 0, delta / np.maximum(maxc, 1e-12), 0.0)
    # 按最大通道分段计算色相，通道差为零时色相置零。
    # Hue: piecewise by which channel is max; delta==0 -> hue 0.
    safe = np.maximum(delta, 1e-12)
    h = np.zeros_like(maxc)
    h = np.where(maxc == r, ((g - b) / safe) % 6.0, h)
    h = np.where(maxc == g, (b - r) / safe + 2.0, h)
    h = np.where(maxc == b, (r - g) / safe + 4.0, h)
    h = np.where(delta > 0, h / 6.0, 0.0)
    return np.stack([h, s, v], axis=-1)


class ColorBlobProvider(BasePerceptionProvider):
    """Largest blob per color prompt. Prompts look like ``color:red``.

    One detection per prompt (this scene has one object per color); the "blob" is
    simply every pixel passing the HSV gate — connected-component analysis would
    need scipy and buys nothing on a clean tabletop.


    每个颜色提示返回一个色块；干净桌面直接使用所有通过 HSV 门的像素，不增加连通域依赖。
    """

    def __init__(self, min_pixels: int = 20) -> None:
        self.min_pixels = min_pixels

    def detect(self, rgb: np.ndarray, prompts: list[str]) -> list[Detection]:
        hsv = rgb_to_hsv(np.asarray(rgb))
        out: list[Detection] = []
        for prompt in prompts:
            color = prompt.split(":", 1)[1] if prompt.startswith("color:") else prompt
            if color not in _COLOR_RANGES:
                continue
            (h_lo, h_hi), s_min, v_min = _COLOR_RANGES[color]
            h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
            hue_ok = (h >= h_lo) | (h <= h_hi) if h_lo > h_hi else (h >= h_lo) & (h <= h_hi)
            mask = hue_ok & (s >= s_min) & (v >= v_min)
            n = int(mask.sum())
            if n < self.min_pixels:
                continue
            ys, xs = np.nonzero(mask)
            out.append(
                Detection(
                    label=prompt,
                    score=min(1.0, n / 500.0),
                    bbox=(int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1),
                    mask=mask,
                )
            )
        return out


class GroundingDinoProvider(BasePerceptionProvider):
    """Open-vocabulary detection (transformers). Heavy import deferred to first use.

    开放词汇检测延迟到首次使用才导入重型库。
    """

    def __init__(self, model_id: str | None = None, threshold: float = 0.4) -> None:
        self.model_id = model_id or os.getenv("PERCEPTION_DINO_MODEL", DEFAULT_DINO_MODEL)
        self.threshold = threshold
        self._bundle = None

    def _load(self):
        if self._bundle is None:
            try:
                import torch  # noqa: F401
                from transformers import AutoModelForZeroShotObjectDetection, AutoProcessor
            except ImportError as e:  # pragma: no cover - only without the perceive group
                raise RuntimeError(
                    "GroundingDinoProvider needs the perceive group: uv sync --group perceive (D015)"
                ) from e
            processor = AutoProcessor.from_pretrained(self.model_id)
            model = AutoModelForZeroShotObjectDetection.from_pretrained(self.model_id)
            model.eval()
            self._bundle = (processor, model)
        return self._bundle

    def detect(self, rgb: np.ndarray, prompts: list[str]) -> list[Detection]:
        import torch

        processor, model = self._load()
        # DINO 查询需要小写，并以点分隔和结束。
        # Grounding DINO expects lowercase queries, "."-terminated and "."-joined.
        text = ". ".join(p.lower().rstrip(".") for p in prompts) + "."
        inputs = processor(images=np.asarray(rgb), text=text, return_tensors="pt")
        with torch.no_grad():
            outputs = model(**inputs)
        h, w = np.asarray(rgb).shape[:2]
        results = processor.post_process_grounded_object_detection(
            outputs, inputs.input_ids, threshold=self.threshold,
            text_threshold=self.threshold, target_sizes=[(h, w)],
        )[0]
        # 兼容不同 transformers 版本的标签键名称。
        # transformers renamed the string-label key over versions; accept both.
        text_labels = results["text_labels"] if "text_labels" in results else results["labels"]
        best: dict[str, Detection] = {}
        for score, label, box in zip(results["scores"], text_labels, results["boxes"]):
            # 按包含关系将命中短语映射回最接近的提示。
            # Map the matched phrase back to the closest prompt (containment match).
            prompt = next((p for p in prompts if label.strip() and label.strip() in p.lower()), None)
            if prompt is None:
                continue
            s = float(score)
            if prompt in best and best[prompt].score >= s:
                continue
            x0, y0, x1, y1 = (int(round(float(v))) for v in box)
            best[prompt] = Detection(label=prompt, score=s, bbox=(x0, y0, x1, y1))
        return list(best.values())


def build_perception_provider(kind: str | None = None) -> BasePerceptionProvider:
    """Factory: ``color`` (default) or ``dino``; env PERCEPTION_PROVIDER overrides.

    工厂默认颜色检测，可由环境选择 DINO。
    """
    kind = (kind or os.getenv("PERCEPTION_PROVIDER") or "color").lower()
    if kind == "color":
        return ColorBlobProvider()
    if kind == "dino":
        return GroundingDinoProvider()
    raise ValueError(f"unknown perception provider {kind!r} (expected 'color' or 'dino')")
