# Ported from car-agent llm-gateway/tests/test_llm_runtime.py @ f0b08f8, changes: imports adapted
# to embodied.providers.*; Redis persistence tests (4) dropped with the feature and replaced by an
# in-memory-only semantics test; ASR/TTS strict-stack test dropped (out of scope); env-scrub list
# extended to every env var the ported code reads; added coverage for cache_scope /
# provider_available / embed_provider (kept-API surface untested upstream).
"""多 LLM 源运行时单测：per-provider body 构造 + 注册表 + 档位解析 + 全局切换。"""
from __future__ import annotations

import asyncio
import os

import pytest

from embodied.providers.llm import MockProvider, OpenAICompatibleProvider
from embodied.providers.runtime import LLMRuntime

_MSG = [{"role": "user", "content": "hi"}]


# ── per-provider body 构造（token 参数名 + 思考风格 + 鉴权）──

def test_build_body_mimo_style():
    p = OpenAICompatibleProvider("k", token_param="max_completion_tokens", thinking_style="mimo")
    body = p._build_body(_MSG, "m", 0.7, 100, thinking=None, stream=False)   # 默认关思考
    assert body["max_completion_tokens"] == 100
    assert body["thinking"] == {"type": "disabled"} and "enable_thinking" not in body
    on = p._build_body(_MSG, "m", 0.7, 100, thinking=True, stream=True)      # 开思考不发键、抬 token
    assert "thinking" not in on and on["max_completion_tokens"] == 2048 and on["stream"] is True


def test_build_body_none_thinking_style():
    # thinking_style="none"：不发任何思考键（用服务商默认）。注：DeepSeek 真栈探测发现其推理模型
    # 认 thinking:{type:disabled}，故 deepseek 实际走 mimo 风格（见 runtime._PROVIDER_SPECS）。
    p = OpenAICompatibleProvider("k", token_param="max_tokens", thinking_style="none")
    body = p._build_body(_MSG, "m", 0.7, 100, thinking=None, stream=False)
    assert body["max_tokens"] == 100
    assert "thinking" not in body and "enable_thinking" not in body


def test_build_body_qwen_style():
    p = OpenAICompatibleProvider("k", token_param="max_tokens", thinking_style="qwen")
    assert p._build_body(_MSG, "m", 0.7, 100, thinking=None, stream=False)["enable_thinking"] is False
    assert p._build_body(_MSG, "m", 0.7, 100, thinking=True, stream=False)["enable_thinking"] is True


def test_auth_headers():
    assert OpenAICompatibleProvider("k", auth_style="bearer")._headers()["Authorization"] == "Bearer k"
    assert OpenAICompatibleProvider("k", auth_style="api-key")._headers()["api-key"] == "k"


# ── 注册表 / 档位解析 / 切换 ──

_ENV_KEYS = ("LLM_PROVIDER", "LLM_API_KEY", "LLM_BASE_URL", "LLM_AUTH_STYLE",
             "LLM_DISABLE_THINKING", "LLM_MODEL_PRIMARY", "LLM_MODEL_FAST", "LLM_MOCK_DELAY_MS",
             "MINIMAX_API_KEY", "MINIMAX_BASE_URL", "MINIMAX_LLM_MODEL",
             "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL_PRIMARY",
             "DEEPSEEK_MODEL_FAST", "DASHSCOPE_LLM_KEY", "DASHSCOPE_ASR_KEY",
             "QWEN_BASE_URL", "QWEN_MODEL_PRIMARY", "QWEN_MODEL_FAST",
             "VISION_MODEL", "VISION_MODEL_FALLBACK",
             "LLM_EMBED_API_KEY", "LLM_EMBED_URL", "LLM_EMBED_MODEL",
             "LLM_EMBED_AUTH_STYLE", "LLM_EMBED_DIMENSIONS",
             "REQUIRE_REAL_PROVIDERS", "REQUIRE_REAL_EXEMPT")


@pytest.fixture(autouse=True)
def _clean_env():
    old = {k: os.environ.get(k) for k in _ENV_KEYS}
    for k in _ENV_KEYS:
        os.environ.pop(k, None)
    yield
    for k, v in old.items():
        os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def _runtime(env: dict) -> LLMRuntime:
    os.environ.update(env)
    return LLMRuntime()


def test_registry_lists_all_greys_unconfigured():
    rt = _runtime({"LLM_PROVIDER": "xiaomimimo", "LLM_API_KEY": "mk", "DEEPSEEK_API_KEY": "dk"})
    st = rt.status()
    by_id = {p["id"]: p for p in st["providers"]}
    assert by_id["mimo"]["available"] and by_id["deepseek"]["available"]
    assert by_id["minimax"]["available"] is False and by_id["qwen"]["available"] is False  # 未配 key 置灰
    assert st["active"]["provider"] == "mimo"                # 默认 xiaomimimo→mimo
    assert rt.resolve_models("") == ["mimo-v2.5-pro", "mimo-v2.5"]
    assert rt.resolve_models("@fast")[0] == "mimo-v2.5"


def test_switch_provider_and_unknown_model_falls_back():
    rt = _runtime({"LLM_PROVIDER": "xiaomimimo", "LLM_API_KEY": "mk", "DEEPSEEK_API_KEY": "dk"})
    rt.set_active("deepseek")
    assert rt.active_id == "deepseek"
    assert rt.resolve_models("")[0] == "deepseek-v4-pro"
    assert rt.resolve_models("@fast")[0] == "deepseek-v4-flash"
    # active=deepseek 时收到 chitchat 发来的 mimo 模型名（不认识）→ 回落 deepseek primary
    assert rt.resolve_models("mimo-v2.5")[0] == "deepseek-v4-pro"
    with pytest.raises(ValueError):        # 切到未配 key 的厂商 → 拒绝
        rt.set_active("qwen")


def test_set_active_specific_model():
    rt = _runtime({"LLM_PROVIDER": "deepseek", "DEEPSEEK_API_KEY": "dk"})
    rt.set_active("deepseek", "deepseek-v4-flash")
    assert rt.resolve_models("")[0] == "deepseek-v4-flash"   # 具体模型覆盖 primary
    assert rt.status()["active"]["model"] == "deepseek-v4-flash"


def test_qwen_reuses_dashscope_key():
    rt = _runtime({"LLM_PROVIDER": "qwen", "DASHSCOPE_ASR_KEY": "bk"})
    assert {p["id"] for p in rt.status()["providers"] if p["available"]} >= {"qwen"}
    assert rt.active_id == "qwen"
    assert rt.resolve_models("")[0] == "qwen3.7-max"


def test_no_keys_falls_back_to_mock():
    rt = _runtime({})
    assert rt.active_id == "mock"
    assert rt.resolve_models("") == ["mock"]


# ── active 仅进程内存（本移植去掉 Redis 持久化：重建回 env 默认）──

def test_set_active_is_in_memory_only():
    rt = _runtime({"LLM_PROVIDER": "xiaomimimo", "LLM_API_KEY": "mk", "DEEPSEEK_API_KEY": "dk"})
    rt.set_active("deepseek", "deepseek-v4-flash")
    assert rt.active_id == "deepseek"
    rt2 = LLMRuntime()                   # 重建（模拟进程重启）
    assert rt2.active_id == "mimo"       # 无持久化 → 回 env 默认
    assert rt2.status()["active"]["model"] == "mimo-v2.5-pro"


def test_cache_scope_tracks_active_provider_and_model():
    rt = _runtime({"LLM_PROVIDER": "xiaomimimo", "LLM_API_KEY": "mk", "DEEPSEEK_API_KEY": "dk"})
    assert rt.cache_scope() == "mimo:mimo-v2.5-pro"
    rt.set_active("deepseek", "deepseek-v4-flash")
    assert rt.cache_scope() == "deepseek:deepseek-v4-flash"


# ── 请求级 pin 的档位解析（运行时硬化 D2）──

def test_resolve_models_for_pinned_provider_uses_its_tiers():
    rt = _runtime({"LLM_PROVIDER": "xiaomimimo", "LLM_API_KEY": "mk", "DEEPSEEK_API_KEY": "dk"})
    assert rt.active_id == "mimo"
    # pin 到非 active 厂商：档位按该厂商词表解析
    assert rt.resolve_models_for("deepseek", "") == ["deepseek-v4-pro", "deepseek-v4-flash"]
    assert rt.resolve_models_for("deepseek", "@fast")[0] == "deepseek-v4-flash"
    # meta.llm_model 覆盖（须在词表内；不在则忽略回落 primary）
    assert rt.resolve_models_for("deepseek", "", "deepseek-v4-flash")[0] == "deepseek-v4-flash"
    assert rt.resolve_models_for("deepseek", "", "mimo-v2.5")[0] == "deepseek-v4-pro"


def test_provider_entry_normalizes_alias():
    rt = _runtime({"LLM_PROVIDER": "xiaomimimo", "LLM_API_KEY": "mk"})
    pid, provider = rt.provider_entry("xiaomimimo")
    assert pid == "mimo" and provider is rt.active_provider()
    assert rt.provider_entry("nope") is None


def test_provider_available_includes_internal_tiers():
    rt = _runtime({"LLM_PROVIDER": "qwen", "DASHSCOPE_LLM_KEY": "bk"})
    assert rt.provider_available("qwen") and rt.provider_available("qwen-vl")
    assert not rt.provider_available("minimax")
    # internal 档（视觉）不进切换列表，但 pin 查询得到
    assert "qwen-vl" not in {p["id"] for p in rt.status()["providers"]}


# ── embedding 解耦 ──

def test_embed_provider_decoupled_from_chat():
    rt = _runtime({"LLM_PROVIDER": "xiaomimimo", "LLM_API_KEY": "mk", "LLM_EMBED_API_KEY": "ek"})
    ep = rt.embed_provider()
    assert isinstance(ep, OpenAICompatibleProvider)
    assert ep.embed_api_key == "ek"
    assert ep.embed_model == "text-embedding-v4"


def test_embed_provider_defaults_to_mock_without_key():
    rt = _runtime({"LLM_PROVIDER": "xiaomimimo", "LLM_API_KEY": "mk"})
    assert isinstance(rt.embed_provider(), MockProvider)


# ── 严格栈（治理 P2：REQUIRE_REAL_PROVIDERS 禁 mock 决议）──

def test_strict_stack_forbids_mock_llm(monkeypatch):
    monkeypatch.setenv("REQUIRE_REAL_PROVIDERS", "on")
    with pytest.raises(RuntimeError, match="REQUIRE_REAL_PROVIDERS"):
        _runtime({})                     # 无任何 chat key → mock active 被拒


def test_strict_stack_forbids_mock_embed(monkeypatch):
    monkeypatch.setenv("REQUIRE_REAL_PROVIDERS", "on")
    with pytest.raises(RuntimeError, match="embed"):
        _runtime({"LLM_PROVIDER": "xiaomimimo", "LLM_API_KEY": "mk"})  # chat 真、embed 缺


def test_strict_stack_exempt_allows_mock(monkeypatch):
    monkeypatch.setenv("REQUIRE_REAL_PROVIDERS", "on")
    monkeypatch.setenv("REQUIRE_REAL_EXEMPT", "llm,embed")
    rt = _runtime({})
    assert rt.active_id == "mock"        # 显式豁免 → 保持现状


# ── 按需探针 + 被动健康（运行时硬化 D5）──

def test_probe_default_active_records_health():
    rt = _runtime({})                   # 无 key → mock provider
    res = asyncio.run(rt.probe(""))
    assert res["ok"] is True and res["provider"] == "mock"
    from embodied.providers.health import health_tracker
    snap = health_tracker.snapshot()
    assert snap["mock"]["ok"] >= 1 and snap["mock"]["ewma_latency_ms"] >= 0
    assert "health" in rt.status()      # status() 附带健康块


def test_probe_unknown_provider_reports_not_configured():
    rt = _runtime({})
    res = asyncio.run(rt.probe("nope"))
    assert res["ok"] is False and "未配置" in res["error"]
