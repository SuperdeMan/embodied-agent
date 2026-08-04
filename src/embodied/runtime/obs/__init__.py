# Ported from car-agent observability/__init__.py @ f0b08f8, changes: metrics module not ported (dropped export); sink-based emitter exports added; setup_structured_logging renamed setup_logging.
"""可观测模块：trace 贯通 + 结构化日志 + 事件发射（可插拔 sink）。"""
from .events import EventEmitter, JsonlSink, Sink, StdoutSink, get_emitter
from .logging import StructuredFormatter, setup_logging
from .tracing import (
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

__all__ = [
    "EventEmitter",
    "JsonlSink",
    "Sink",
    "StdoutSink",
    "StructuredFormatter",
    "get_emitter",
    "get_session_id",
    "get_trace_id",
    "inject_trace_meta",
    "new_trace_id",
    "set_session_id",
    "set_trace_id",
    "setup_logging",
    "setup_tracing",
    "start_span",
    "trace_context_from_meta",
]
