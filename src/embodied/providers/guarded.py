"""GuardedProvider: cache + rate limiting around a BaseProvider, duck-type transparent.

Wires the M0-ported LLMCache/RateLimiter into the serving path (roadmap M1 item).
Wrap REAL providers only — the offline ScriptedToolProvider is stateful (sequence
draining) and deterministic; caching it would replay stale plans.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from embodied.providers.cache import LLMCache
from embodied.providers.ratelimit import RateLimiter


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


class RateLimited(RuntimeError):
    pass


class GuardedProvider:
    """Token-bucket admission on complete/complete_tools + TTL cache on complete only.

    The ported LLMCache stores (content, model_used) and reconstructs the rest — it cannot
    faithfully represent a complete_tools 5-tuple (tool_calls would be silently dropped),
    so planning calls are rate-limited but never cached. Streaming passes through untouched.
    """

    def __init__(self, inner: Any, *, cache: LLMCache | None = None, limiter: RateLimiter | None = None):
        self.inner = inner
        enabled = os.getenv("LLM_CACHE", "on").strip().lower() != "off"
        ttl = int(_env_float("LLM_CACHE_TTL_S", 120))
        self._cache = cache if cache is not None else (LLMCache(ttl_seconds=ttl) if enabled else None)
        self._limiter = limiter if limiter is not None else RateLimiter(
            global_rate=_env_float("LLM_RATE", 5), global_capacity=_env_float("LLM_BURST", 10)
        )
        self._wait_cap_s = _env_float("LLM_RATE_WAIT_CAP_S", 5.0)

    async def _admit(self, key: str) -> None:
        waited = 0.0
        while not self._limiter.allow(key):
            if waited >= self._wait_cap_s:
                raise RateLimited(f"rate limit exceeded for {key}")
            await asyncio.sleep(0.2)
            waited += 0.2

    async def complete(self, messages, model, temperature, max_tokens, thinking=None, timeout_s=None):
        if self._cache is not None:
            hit = self._cache.get(messages, model, temperature, thinking)
            if hit is not None:
                return hit
        await self._admit("complete")
        content, model_used, finish, usage = await self.inner.complete(
            messages, model, temperature, max_tokens, thinking=thinking, timeout_s=timeout_s
        )
        if self._cache is not None and finish == "stop":
            self._cache.put(messages, model, temperature, content, model_used, thinking)
        return content, model_used, finish, usage

    async def complete_tools(
        self, messages, model, temperature, max_tokens,
        tools=None, tool_choice=None, thinking=None, timeout_s=None,
    ):
        await self._admit("complete_tools")
        return await self.inner.complete_tools(
            messages, model, temperature, max_tokens,
            tools=tools, tool_choice=tool_choice, thinking=thinking, timeout_s=timeout_s,
        )

    def __getattr__(self, name: str) -> Any:  # stream/embed and anything else: pass through
        return getattr(self.inner, name)
