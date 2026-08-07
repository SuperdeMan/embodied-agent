"""embodied.providers — LLM provider layer ported from car-agent llm-gateway (M0).

Public surface: provider classes + build_provider() (llm.py), LLMRuntime/get_runtime()
(runtime.py), LLMCache (cache.py), RateLimiter/TokenBucket (ratelimit.py),
ProviderHealth/health_tracker (health.py), ASR/TTS providers + build_asr_provider()/
build_tts_provider()/build_streaming_asr_provider()/build_tts_stream_provider() (audio.py, M1).
S2S stays out of scope.
"""
from .audio import (
    TTS_STREAM_CATALOG,
    BaseASRProvider,
    BaseStreamingASRProvider,
    BaseStreamingTTSProvider,
    BaseTTSProvider,
    DashScopeCosyVoiceProvider,
    DashScopeInferenceASRProvider,
    DashScopeQwenTTSProvider,
    DashScopeRealtimeASRProvider,
    MiMoASRProvider,
    MiMoChunkedASRProvider,
    MiMoStreamingTTSProvider,
    MiMoTTSProvider,
    MiniMaxStreamingTTSProvider,
    MockASRProvider,
    MockStreamingTTSProvider,
    MockTTSProvider,
    StreamBridgeASRProvider,
    StreamBridgeTTSProvider,
    build_asr_provider,
    build_streaming_asr_provider,
    build_tts_provider,
    build_tts_stream_provider,
    emotion_instruct,
)
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
from .policy import (
    BasePolicyProvider,
    MockChunkPolicy,
    OnnxPolicy,
    PolicyMeta,
    build_policy_provider,
)
from .ratelimit import RateLimiter, TokenBucket
from .runtime import LLMRuntime, get_runtime

__all__ = [
    "AnthropicProvider",
    "BaseASRProvider",
    "BasePolicyProvider",
    "BaseProvider",
    "BaseStreamingASRProvider",
    "BaseStreamingTTSProvider",
    "BaseTTSProvider",
    "DashScopeCosyVoiceProvider",
    "DashScopeInferenceASRProvider",
    "DashScopeQwenTTSProvider",
    "DashScopeRealtimeASRProvider",
    "LLMCache",
    "LLMRuntime",
    "MiMoASRProvider",
    "MiMoChunkedASRProvider",
    "MiMoStreamingTTSProvider",
    "MiMoTTSProvider",
    "MiniMaxStreamingTTSProvider",
    "MockASRProvider",
    "MockChunkPolicy",
    "MockProvider",
    "MockStreamingTTSProvider",
    "MockTTSProvider",
    "OnnxPolicy",
    "OpenAICompatibleProvider",
    "PolicyMeta",
    "ProviderHTTPError",
    "ProviderHealth",
    "RateLimiter",
    "StreamBridgeASRProvider",
    "StreamBridgeTTSProvider",
    "TTS_STREAM_CATALOG",
    "ThinkStreamStripper",
    "TokenBucket",
    "build_asr_provider",
    "build_policy_provider",
    "build_provider",
    "build_streaming_asr_provider",
    "build_tts_provider",
    "build_tts_stream_provider",
    "emotion_instruct",
    "get_runtime",
    "health_tracker",
    "normalize_tool_calls",
    "strip_think_block",
]
