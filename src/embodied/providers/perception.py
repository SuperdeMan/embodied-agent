"""PerceptionProvider family: 2D open-vocabulary detection (docs/decisions.md D015).

Providers only do image -> detections; 3D pose estimation lives in
``cognition/perception.py`` (depth back-projection), keeping the provider seam
identical for sim renders today and real RGB-D frames in M3.

Two providers ship in v1:
- ``ColorBlobProvider`` — pure-numpy HSV thresholding. Zero deps, offline, fast:
  the CI-testable fallback that proves the perception CHAIN without any weights.
- ``GroundingDinoProvider`` — open-vocabulary detection via transformers
  (``perceive`` dependency group); model id overridable by env.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import numpy as np

DEFAULT_DINO_MODEL = "IDEA-Research/grounding-dino-tiny"

# HSV ranges for the color: prompt convention "color:<name>". Hue in [0, 1).
_COLOR_RANGES: dict[str, tuple[tuple[float, float], float, float]] = {
    # name -> ((hue_lo, hue_hi) wrapping allowed, min saturation, min value)
    "red": ((0.93, 0.07), 0.45, 0.25),
    "green": ((0.25, 0.45), 0.35, 0.2),
    "blue": ((0.55, 0.72), 0.35, 0.2),
    "yellow": ((0.12, 0.2), 0.35, 0.3),
}


@dataclass
class Detection:
    label: str  # the prompt this detection answers
    score: float
    bbox: tuple[int, int, int, int]  # x0, y0, x1, y1 (pixels, half-open)
    mask: np.ndarray | None = field(default=None, repr=False)  # HxW bool, optional


class BasePerceptionProvider(ABC):
    @abstractmethod
    def detect(self, rgb: np.ndarray, prompts: list[str]) -> list[Detection]:
        """``rgb`` HxWx3 uint8; returns at most one best detection per prompt."""


def rgb_to_hsv(rgb: np.ndarray) -> np.ndarray:
    """Vectorized RGB(uint8) -> HSV(float in [0,1]); mirrors colorsys semantics."""
    arr = rgb.astype(np.float32) / 255.0
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    maxc = arr.max(axis=-1)
    minc = arr.min(axis=-1)
    v = maxc
    delta = maxc - minc
    s = np.where(maxc > 0, delta / np.maximum(maxc, 1e-12), 0.0)
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
    """Open-vocabulary detection (transformers). Heavy import deferred to first use."""

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
        # transformers renamed the string-label key over versions; accept both.
        text_labels = results["text_labels"] if "text_labels" in results else results["labels"]
        best: dict[str, Detection] = {}
        for score, label, box in zip(results["scores"], text_labels, results["boxes"]):
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
    """Factory: ``color`` (default) or ``dino``; env PERCEPTION_PROVIDER overrides."""
    kind = (kind or os.getenv("PERCEPTION_PROVIDER") or "color").lower()
    if kind == "color":
        return ColorBlobProvider()
    if kind == "dino":
        return GroundingDinoProvider()
    raise ValueError(f"unknown perception provider {kind!r} (expected 'color' or 'dino')")
