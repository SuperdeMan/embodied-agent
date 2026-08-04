# Ported from car-agent observability/tests/test_otel.py @ f0b08f8, changes: MetricsCollector tests dropped (metrics module not ported); added session-id + optional-OTel-fallback coverage.
"""tracing：trace/session contextvar、meta 注入、OTel 可选降级。"""
import asyncio

import pytest

from embodied.runtime.obs.tracing import (
    get_session_id,
    get_trace_id,
    inject_trace_meta,
    new_trace_id,
    set_session_id,
    set_trace_id,
    setup_tracing,
    start_span,
    trace_context_from_meta,
)


@pytest.fixture(autouse=True)
def _clean_ctx():
    set_trace_id("")
    set_session_id("")
    yield
    set_trace_id("")
    set_session_id("")


def test_trace_id_roundtrip():
    tid = new_trace_id()
    assert len(tid) == 16
    set_trace_id(tid)
    assert get_trace_id() == tid


def test_inject_trace_meta():
    set_trace_id("abc123")
    meta = {}
    inject_trace_meta(meta)
    assert meta["trace_id"] == "abc123"


def test_inject_trace_meta_noop_without_trace():
    meta = {}
    inject_trace_meta(meta)
    assert meta == {}


def test_trace_id_is_isolated_between_async_tasks():
    async def worker(trace_id):
        set_trace_id(trace_id)
        await asyncio.sleep(0)
        return get_trace_id()

    async def run():
        return await asyncio.gather(worker("trace-a"), worker("trace-b"))

    assert asyncio.run(run()) == ["trace-a", "trace-b"]


def test_session_id_roundtrip_and_none_clears():
    set_session_id("sess-1")
    assert get_session_id() == "sess-1"
    set_session_id(None)
    assert get_session_id() == ""


def test_trace_context_from_meta_uses_existing_id():
    tid = trace_context_from_meta({"trace_id": "given-1"})
    assert tid == "given-1"
    assert get_trace_id() == "given-1"


def test_trace_context_from_meta_generates_when_missing():
    tid = trace_context_from_meta({})
    assert len(tid) == 16
    assert get_trace_id() == tid


def test_start_span_is_noop_context_manager_without_otel():
    with start_span("planner.step"):
        pass


def test_setup_tracing_without_endpoint_falls_back(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert setup_tracing("test-svc") is True


def test_setup_tracing_with_endpoint_but_no_otel_sdk(monkeypatch):
    """OTel 不是硬依赖：设了 endpoint 但没装 SDK → 降级简化版，不抛。

    本仓库刻意不装 opentelemetry（optional-guarded only），此测试同时钉住这一点。
    """
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4317")
    assert setup_tracing("test-svc") is True
    set_trace_id("still-contextvar")
    assert get_trace_id() == "still-contextvar"
