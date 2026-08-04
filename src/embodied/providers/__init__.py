"""embodied.providers — LLM provider layer ported from car-agent llm-gateway (M0).

Public surface: provider classes + build_provider() (llm.py), LLMRuntime/get_runtime()
(runtime.py), LLMCache (cache.py), RateLimiter/TokenBucket (ratelimit.py),
ProviderHealth/health_tracker (health.py). ASR/TTS/S2S are out of scope until M1.
"""
from .cache import LLMCache
from .health import ProviderHealth, health_tracker
from .llm import (
    AnthropicProvider,
    BaseProvider,
    MockProvider,
    OpenAICompatibleProvider,
    ProviderHTTPError,
    ThinkStreamStripper,
    build_provider,
    normalize_tool_calls,
    strip_think_block,
)
from .ratelimit import RateLimiter, TokenBucket
from .runtime import LLMRuntime, get_runtime

__all__ = [
    "AnthropicProvider",
    "BaseProvider",
    "LLMCache",
    "LLMRuntime",
    "MockProvider",
    "OpenAICompatibleProvider",
    "ProviderHTTPError",
    "ProviderHealth",
    "RateLimiter",
    "ThinkStreamStripper",
    "TokenBucket",
    "build_provider",
    "get_runtime",
    "health_tracker",
    "normalize_tool_calls",
    "strip_think_block",
]
