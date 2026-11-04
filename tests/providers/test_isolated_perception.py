"""Isolated detections preserve the reference provider output. 隔离检测保持参考提供方输出。"""

import os

import numpy as np
import pytest

from embodied.providers.perception import ColorBlobProvider, build_perception_provider


def test_color_masks_scores_and_labels_match_reference():
    rgb = np.zeros((80, 100, 3), dtype=np.uint8)
    rgb[10:40, 10:30] = [255, 0, 0]
    rgb[45:65, 50:80] = [0, 255, 0]
    prompts = ["color:red", "color:green", "color:blue"]
    process = build_perception_provider("color", isolated=True)
    try:
        assert process.worker.last_pid != os.getpid()
        actual, expected = process.detect(rgb, prompts), ColorBlobProvider().detect(rgb, prompts)
        assert len(actual) == len(expected)
        for a, b in zip(actual, expected):
            assert (a.label, a.score, a.bbox) == (b.label, b.score, b.bbox)
            np.testing.assert_array_equal(a.mask, b.mask)
        with pytest.raises(ValueError):
            process.detect(rgb.astype(np.float32), prompts)
        with pytest.raises(ValueError):
            process.detect(rgb, ["color:red", "color:red"])
        assert process.worker.state == "READY"
    finally:
        process.close()
    assert process.worker.reaped
