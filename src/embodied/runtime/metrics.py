"""Lightweight thread-safe latency and counter collectors for P1 observability (D035).

P1 可观测性轻量线程安全延迟与计数采集器（D035）：零外部依赖、不阻塞控制与安全路径。
"""

from __future__ import annotations

import math
import threading
from collections import deque

from embodied.contracts.metrics import LatencySummary

STALE_REJECTION_REASONS: frozenset[str] = frozenset({
    "action_expired_or_invalid_deadline",
    "control_observation_stale",
    "semantic_observation_stale",
    "semantic_target_stale",
})

REPLAY_REJECTION_REASONS: frozenset[str] = frozenset({
    "action_sequence_replayed",
})

EPOCH_MISMATCH_REASONS: frozenset[str] = frozenset({
    "action_identity_or_layout_mismatch",
    "invocation_epoch_mismatch",
    "observation_reference_invalid",
    "control_profile_changed",
})


def summarize_latencies_ms(samples_ms: list[float] | tuple[float, ...]) -> LatencySummary:
    """Compute exact nearest-rank min/p50/p95/max milliseconds from finite samples.

    基于有限正值样本计算最近秩 min / p50 / p95 / max 毫秒分布。
    """
    clean = sorted(float(v) for v in samples_ms)
    if any(not math.isfinite(v) or v < 0.0 for v in clean):
        raise ValueError("latency samples must be finite and non-negative")
    n = len(clean)
    if n == 0:
        return LatencySummary(count=0)

    def _pct(q: float) -> float:
        idx = max(0, min(n - 1, math.ceil(n * q) - 1))
        return round(clean[idx], 4)

    return LatencySummary(
        count=n,
        min_ms=round(clean[0], 4),
        p50_ms=_pct(0.50),
        p95_ms=_pct(0.95),
        max_ms=round(clean[-1], 4),
    )


class LatencyRecorder:
    """Bounded thread-safe ring buffer of named millisecond latency observations.

    有界线程安全延迟样本环缓存，用于记录急停闩锁、强杀与停止观测耗时。
    """

    def __init__(self, max_samples: int = 1024) -> None:
        if type(max_samples) is not int or max_samples <= 0:
            raise ValueError("max_samples must be a positive integer")
        self._max_samples = max_samples
        self._lock = threading.Lock()
        self._series: dict[str, deque[float]] = {}

    def record_ms(self, name: str, elapsed_ms: float) -> None:
        val = float(elapsed_ms)
        if not math.isfinite(val) or val < 0.0:
            return
        with self._lock:
            buf = self._series.get(name)
            if buf is None:
                buf = deque(maxlen=self._max_samples)
                self._series[name] = buf
            buf.append(val)

    def samples(self, name: str) -> tuple[float, ...]:
        with self._lock:
            return tuple(self._series.get(name, ()))

    def summary(self, name: str) -> LatencySummary:
        return summarize_latencies_ms(self.samples(name))

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            names = sorted(self._series)
        return {name: self.summary(name).model_dump(mode="json") for name in names}
