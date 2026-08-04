"""GuardedProvider: cache + rate-limit wiring around the provider contract."""

from __future__ import annotations

import pytest

from embodied.providers.cache import LLMCache
from embodied.providers.guarded import GuardedProvider, RateLimited
from embodied.providers.ratelimit import RateLimiter


class CountingProvider:
    def __init__(self):
        self.completes = 0
        self.tool_calls = 0

    async def complete(self, messages, model, temperature, max_tokens, thinking=None, timeout_s=None):
        self.completes += 1
        return f"r{self.completes}", "m", "stop", (1, 1)

    async def complete_tools(self, messages, model, temperature, max_tokens,
                             tools=None, tool_choice=None, thinking=None, timeout_s=None):
        self.tool_calls += 1
        return "", "m", "tool_use", (1, 1), [{"id": "1", "name": "t", "arguments": {}}]


MSGS = [{"role": "user", "content": "hi"}]


async def test_cache_hit_skips_inner():
    inner = CountingProvider()
    g = GuardedProvider(inner, cache=LLMCache(ttl_seconds=60), limiter=RateLimiter(global_rate=100, global_capacity=100))
    r1 = await g.complete(MSGS, "m", 0.1, 64)
    r2 = await g.complete(MSGS, "m", 0.1, 64)
    assert r1[0] == r2[0] and inner.completes == 1  # content served from cache
    r3 = await g.complete([{"role": "user", "content": "other"}], "m", 0.1, 64)
    assert r3[0] == "r2" and inner.completes == 2  # different messages miss


async def test_complete_tools_never_cached():
    """The ported cache cannot represent tool_calls; planning calls must always hit the
    provider (a silently dropped tool_calls list would look like 'no plan')."""
    inner = CountingProvider()
    g = GuardedProvider(inner, cache=LLMCache(ttl_seconds=60), limiter=RateLimiter(global_rate=100, global_capacity=100))
    out1 = await g.complete_tools(MSGS, "m", 0.1, 64, tools=[{"a": 1}])
    out2 = await g.complete_tools(MSGS, "m", 0.1, 64, tools=[{"a": 1}])
    assert inner.tool_calls == 2
    assert out1[4] and out2[4]  # tool_calls intact both times


async def test_rate_limit_raises_after_wait_cap(monkeypatch):
    monkeypatch.setenv("LLM_RATE_WAIT_CAP_S", "0.3")
    inner = CountingProvider()
    g = GuardedProvider(inner, cache=None, limiter=RateLimiter(global_rate=0.001, global_capacity=1))
    await g.complete(MSGS, "m", 0.1, 64)  # consumes the single token
    with pytest.raises(RateLimited):
        await g.complete([{"role": "user", "content": "again"}], "m", 0.1, 64)


async def test_passthrough_getattr():
    inner = CountingProvider()
    inner.custom_attr = 42
    assert GuardedProvider(inner).custom_attr == 42
