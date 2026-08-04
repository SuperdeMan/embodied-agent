# New tests written for the embodied-agent port (no car-agent counterpart): cover surface the
# upstream suite left untested at provider level — mock contract, complete()/stream() over a mocked
# HTTP transport, structured HTTP errors + Retry-After, and build_provider() env dispatch.
"""补充单测：MockProvider 契约、HTTP 层（httpx.MockTransport，不打网络）、build_provider 装配。"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from embodied.providers.llm import (
    AnthropicProvider,
    MockProvider,
    OpenAICompatibleProvider,
    ProviderHTTPError,
    _retry_after_s,
    build_provider,
)

_MSG = [{"role": "user", "content": "hi"}]


def _provider_with(handler, **kw):
    p = OpenAICompatibleProvider("k", base_url="http://test.local/v1/chat/completions", **kw)
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return p


# ── MockProvider 契约（planner 依赖的四元组/流式形状）──

def test_mock_complete_contract_and_stream_equivalence():
    p = MockProvider()
    content, used, finish, usage = asyncio.run(
        p.complete([{"role": "user", "content": "hello"}], "any", 0.5, 64))
    assert used == "mock" and finish == "stop" and usage == (0, 0)
    assert "hello" in content

    async def collect():
        return [c async for c in p.stream([{"role": "user", "content": "hello"}], "any", 0.5, 64)]

    assert "".join(asyncio.run(collect())) == content


# ── complete()：<think> 剥离 + usage 四元组（HTTP 层 mock）──

def test_complete_strips_think_and_returns_usage():
    def handler(request):
        assert request.headers["api-key"] == "k"      # 默认 api-key 鉴权风格
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "<think>internal</think>\n\nanswer"}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3}})

    p = _provider_with(handler)
    content, used, finish, usage = asyncio.run(p.complete(_MSG, "m", 0.2, 64))
    assert (content, used, finish, usage) == ("answer", "m", "stop", (7, 3))


# ── 上游 HTTP 错误结构化（状态码 + body 片段 + Retry-After）──

def test_http_error_carries_status_snippet_and_retry_after():
    def handler(request):
        return httpx.Response(429, text="rate limited\nslow down",
                              headers={"retry-after": "1.5"})

    p = _provider_with(handler)
    with pytest.raises(ProviderHTTPError) as ei:
        asyncio.run(p.complete(_MSG, "m", 0.2, 64))
    e = ei.value
    assert e.status_code == 429
    assert e.retry_after == 1.5
    assert "provider HTTP 429" in str(e) and "rate limited slow down" in str(e)  # 换行已压平


def test_retry_after_parsing_edge_cases():
    class _Resp:
        def __init__(self, headers):
            self.headers = headers

    assert _retry_after_s(_Resp({})) is None
    assert _retry_after_s(_Resp({"retry-after": "2"})) == 2.0
    assert _retry_after_s(_Resp({"retry-after": "-3"})) == 0.0   # 负值钳到 0
    assert _retry_after_s(_Resp({"retry-after": "soon"})) is None  # HTTP-date 形式按无处理
    assert _retry_after_s(object()) is None                      # 无 headers 的桩防御


# ── stream()：SSE 解析 + 跨 chunk <think> 剥离 + 错误结构化 ──

def test_stream_sse_strips_think_across_chunks():
    lines = []
    for delta in ["<th", "ink>reason</think>\n\nans", "wer"]:
        lines.append("data: " + json.dumps({"choices": [{"delta": {"content": delta}}]}))
    lines.append("data: [DONE]")
    payload = ("\n".join(lines) + "\n").encode()

    def handler(request):
        body = json.loads(request.content.decode())
        assert body["stream"] is True
        return httpx.Response(200, content=payload)

    p = _provider_with(handler)

    async def collect():
        return [c async for c in p.stream(_MSG, "m", 0.2, 64)]

    assert "".join(asyncio.run(collect())) == "answer"


def test_stream_http_error_raises_structured():
    def handler(request):
        return httpx.Response(500, text="boom")

    p = _provider_with(handler)

    async def drain():
        async for _ in p.stream(_MSG, "m", 0.2, 64):
            pass

    with pytest.raises(ProviderHTTPError) as ei:
        asyncio.run(drain())
    assert ei.value.status_code == 500 and "boom" in str(ei.value)


# ── build_provider()：env 装配分发 ──

def test_build_provider_env_dispatch(monkeypatch):
    for k in ("LLM_PROVIDER", "LLM_API_KEY", "LLM_BASE_URL", "LLM_AUTH_STYLE",
              "LLM_DISABLE_THINKING", "LLM_EMBED_URL", "LLM_EMBED_MODEL",
              "LLM_EMBED_API_KEY", "LLM_EMBED_AUTH_STYLE", "LLM_EMBED_DIMENSIONS",
              # AsyncAnthropic 构造时按 env 建 httpx client：宿主机的代理配置（如 SOCKS）
              # 会让离线测试去装可选依赖——清掉，保证不碰网络也不受宿主 env 影响
              "ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy",
              "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(k, raising=False)
    assert isinstance(build_provider(), MockProvider)          # 无 key → mock 兜底

    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    assert isinstance(build_provider(), AnthropicProvider)     # anthropic 走独立 SDK

    monkeypatch.setenv("LLM_PROVIDER", "deepseek")             # 其余一律 OpenAI 兼容
    monkeypatch.setenv("LLM_BASE_URL", "https://x/v1/chat/completions")
    monkeypatch.setenv("LLM_AUTH_STYLE", "bearer")
    monkeypatch.setenv("LLM_DISABLE_THINKING", "false")
    p = build_provider()
    assert isinstance(p, OpenAICompatibleProvider)
    assert p.base_url == "https://x/v1/chat/completions"
    assert p.auth_style == "bearer"
    assert p.disable_thinking is False
