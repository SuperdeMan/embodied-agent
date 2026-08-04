# Ported from car-agent observability/events.py @ f0b08f8, changes: NATS transport replaced by pluggable Sink protocol (JsonlSink default, StdoutSink via OBS_STDOUT=1); emit_* API surface unchanged; state subject renamed robot.state.changed; change_source default now "robot"; get_emitter default service now "runtime".
"""Best-effort observability event publishing to pluggable sinks.

Transport redesign (M0): the origin published events over NATS to a separate
collector service. Here events go to in-process sinks instead:

- ``Sink`` protocol: ``emit(subject: str, payload: dict) -> None`` —
  **synchronous by contract**. Sinks are invoked from the emitter's
  background worker task, never from the caller's await path, so a slow
  sink cannot stall the primary request path. Sink exceptions are caught
  and dropped (logged at DEBUG); emitting never raises into the caller.
- ``JsonlSink`` (default): appends one JSON object per line to
  ``$OBS_DIR/events.jsonl`` (env ``OBS_DIR``, default ``outputs/obs``
  relative to the current working directory; directory auto-created).
  Line shape: ``{"subject": <subject>, ...payload}``.
- ``StdoutSink`` (optional): enabled with env ``OBS_STDOUT=1``; writes the
  same JSON lines to stdout.

The public ``emit_*`` coroutine API is identical to the origin so call
sites port unchanged.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
import os
import sys
import time
import uuid
from typing import Protocol

logger = logging.getLogger("obs.events")
_QUEUE_LIMIT = 1000

change_source: contextvars.ContextVar[str] = contextvars.ContextVar(
    "change_source",
    default="robot",
)


def _now_ms() -> int:
    return int(time.time() * 1000)


class Sink(Protocol):
    """Pluggable event sink. ``emit`` is synchronous and is only ever called
    from the emitter's background worker; it may raise — the emitter catches
    and drops (never propagates to the emitting caller)."""

    def emit(self, subject: str, payload: dict) -> None: ...


class JsonlSink:
    """Append events as JSON Lines to ``$OBS_DIR/events.jsonl``.

    ``OBS_DIR`` defaults to ``outputs/obs`` (relative to cwd); the directory
    is auto-created on first write.
    """

    def __init__(self, obs_dir: str | None = None):
        if obs_dir is None:
            obs_dir = os.getenv("OBS_DIR", "") or os.path.join("outputs", "obs")
        self._dir = obs_dir

    @property
    def path(self) -> str:
        return os.path.join(self._dir, "events.jsonl")

    def emit(self, subject: str, payload: dict) -> None:
        os.makedirs(self._dir, exist_ok=True)
        line = json.dumps({"subject": subject, **payload}, ensure_ascii=False)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


class StdoutSink:
    """Mirror events to stdout as JSON lines (enabled by env ``OBS_STDOUT=1``)."""

    def emit(self, subject: str, payload: dict) -> None:
        # 每次调用取当前 sys.stdout：测试的 capsys 替换后仍然生效。
        sys.stdout.write(json.dumps({"subject": subject, **payload}, ensure_ascii=False) + "\n")


def default_sinks() -> list[Sink]:
    """Build the env-configured default sink set (JSONL always; stdout opt-in)."""
    sinks: list[Sink] = [JsonlSink()]
    if os.getenv("OBS_STDOUT", "").strip().lower() in {"1", "true", "on"}:
        sinks.append(StdoutSink())
    return sinks


class EventEmitter:
    """Publish observability events without affecting the primary request path."""

    def __init__(self, service: str, sinks: list[Sink] | None = None):
        self.service = service
        self.sinks: list[Sink] = default_sinks() if sinks is None else list(sinks)
        self._disabled = not self.sinks
        self._queue: asyncio.Queue[tuple[str, dict]] = asyncio.Queue(
            maxsize=_QUEUE_LIMIT
        )
        self._worker_task: asyncio.Task | None = None

    def _deliver(self, subject: str, payload: dict) -> None:
        """Fan out one event to all sinks; each failure is logged and dropped."""
        for sink in self.sinks:
            try:
                sink.emit(subject, payload)
            except Exception as exc:
                logger.debug("sink %s emit %s failed: %s",
                             type(sink).__name__, subject, exc)

    async def _run_worker(self) -> None:
        while True:
            subject, payload = await self._queue.get()
            try:
                self._deliver(subject, payload)
            finally:
                self._queue.task_done()

    def _ensure_worker(self) -> None:
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(
                self._run_worker(),
                name=f"obs-events-{self.service}",
            )

    async def _emit(self, subject: str, payload: dict) -> None:
        if self._disabled:
            return
        payload.setdefault("ts", _now_ms())
        payload.setdefault("service", self.service)
        # 会话维度自动携带：请求入口 set_session_id 一次，所有事件免逐点透传。
        # 后台任务（无请求上下文）取到空串则不注入，保持事件干净。
        if not payload.get("session_id"):
            from .tracing import get_session_id

            sid = get_session_id()
            if sid:
                payload["session_id"] = sid
        try:
            self._queue.put_nowait((subject, payload))
        except asyncio.QueueFull:
            logger.debug("observability queue full; dropped %s", subject)
            return
        self._ensure_worker()

    async def emit_span(
        self,
        trace_id,
        node,
        status="ok",
        duration_ms=0,
        attrs=None,
        parent_id="",
        span_id="",
    ) -> None:
        await self._emit(
            "obs.span",
            {
                "trace_id": trace_id,
                "span_id": span_id or uuid.uuid4().hex[:12],
                "parent_id": parent_id,
                "node": node,
                "status": status,
                "duration_ms": round(duration_ms, 1),
                "attrs": attrs or {},
            },
        )

    async def emit_turn(
        self,
        trace_id,
        session_id,
        *,
        user_text="",
        speech="",
        status="ok",
        path="",
        input_source="",
        is_confirmation=False,
        ui_card_type="",
        actions=0,
        duration_ms=0,
        error="",
        ts=None,
    ) -> None:
        """轮次收口事件（badcase 排查核心）：一次请求处理 = 一条 turn。

        内容字段（user_text/speech）经 OBS_CONTENT_CAPTURE 门控 + 统一脱敏；
        error 恒脱敏（异常串可能夹带敏感参数）。ts 传请求开始时刻（缺省=发射时刻）。
        """
        from .redact import gate_content, redact

        payload = {
            "trace_id": trace_id,
            "session_id": session_id,
            "user_text": gate_content(user_text, 500),
            "speech": gate_content(speech, 1000),
            "status": status,
            "path": path,
            "input_source": input_source,
            "is_confirmation": bool(is_confirmation),
            "ui_card_type": ui_card_type,
            "actions": actions,
            "duration_ms": round(duration_ms, 1),
            "error": redact(error)[:300] if error else "",
        }
        if ts is not None:
            payload["ts"] = ts
        await self._emit("obs.turn", payload)

    async def emit_llm(
        self,
        *,
        trace_id="",
        session_id="",
        caller="",
        model="",
        provider="",
        requested_tier="",
        pinned=False,
        prompt_tokens=0,
        completion_tokens=0,
        latency_ms=0,
        cache_hit=False,
        thinking=False,
        status="ok",
        error="",
        prompt_tail="",
        content_head="",
    ) -> None:
        """LLM 调用事件（provider 网关唯一出口收口）：模型/tokens/时延/缓存按 trace 归档。
        provider=实际 serving 的厂商 id；requested_tier=调用方原始 model 参数（""/@fast/具体名，
        审计谁在用什么档）；pinned=本次调用是否被请求级锁定。
        prompt_tail/content_head 受 OBS_CONTENT_CAPTURE 门控 + 脱敏。"""
        from .redact import gate_content, redact

        await self._emit(
            "obs.llm",
            {
                "trace_id": trace_id,
                "session_id": session_id,
                "caller": caller,
                "model": model,
                "provider": provider,
                "requested_tier": requested_tier,
                "pinned": bool(pinned),
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "latency_ms": round(latency_ms, 1),
                "cache_hit": bool(cache_hit),
                "thinking": bool(thinking),
                "status": status,
                "error": redact(error)[:300] if error else "",
                "prompt_tail": gate_content(prompt_tail, 500),
                "content_head": gate_content(content_head, 800),
            },
        )

    async def emit_state(self, changes, source, trace_id="") -> None:
        await self._emit(
            "robot.state.changed",
            {
                "trace_id": trace_id,
                "source": source,
                "changes": changes,
            },
        )

    async def emit_metric(
        self,
        agent_id,
        count,
        avg_ms,
        error_rate,
        **extra,
    ) -> None:
        await self._emit(
            "obs.metric",
            {
                "agent_id": agent_id,
                "count": count,
                "avg_ms": avg_ms,
                "error_rate": error_rate,
                **extra,
            },
        )

    async def emit_health(
        self,
        agent_id,
        healthy,
        fail_count,
        last_seen,
        deployment="",
        kind="",
    ) -> None:
        await self._emit(
            "obs.agent.health",
            {
                "agent_id": agent_id,
                "healthy": healthy,
                "fail_count": fail_count,
                "last_seen": last_seen,
                "deployment": deployment,
                "kind": kind,
            },
        )

    async def close(self) -> None:
        if self._worker_task is not None:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.flush(), timeout=1)
            self._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker_task
            self._worker_task = None

    async def flush(self) -> None:
        """Wait until queued events have been delivered or dropped."""
        await self._queue.join()


_default_emitters: dict[str, EventEmitter] = {}


def get_emitter(service: str = "runtime") -> EventEmitter:
    """Return one best-effort emitter per service in the current process."""
    emitter = _default_emitters.get(service)
    if emitter is None:
        emitter = EventEmitter(service)
        _default_emitters[service] = emitter
    return emitter
