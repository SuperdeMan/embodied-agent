# Ported from car-agent llm-gateway/tests/test_batch_audio_providers.py @ f0b08f8, changes:
# imports adapted to embodied.providers.audio; dropped test_mimo_catalog_selects_real_female_and_male
# (depends on llm-gateway scripts/prepare_voiceprint_fixtures, not ported) and
# test_complete_4xx_error_carries_response_body (LLM section, covered by llm tests); _clean_env list
# extended (ASR_STREAM_*, TTS_STREAM_MODEL, REQUIRE_REAL_PROVIDERS) for hermeticity; fake-ASR sample
# text swapped to a desktop-manipulation phrase (car-agent domain wording dropped); added offline
# Mock ASR/TTS contract tests (they back the keyless console demo).
"""批处理 ASR/TTS 工厂 + 流式桥接 单测——全部离线可跑。

背景：批处理面此前硬绑 MiMo，chat 换家（LLM_PROVIDER≠mimo 系）即静默降级 Mock。
本组用例固化契约：ASR_PROVIDER/TTS_PROVIDER 显式可配 + auto 下桥接流式引擎，
不用 MiMo 也有真 ASR/TTS。
"""
from __future__ import annotations

import asyncio

import embodied.providers.audio as P
from embodied.providers.audio import (
    MiMoASRProvider,
    MiMoTTSProvider,
    MockASRProvider,
    MockTTSProvider,
    StreamBridgeASRProvider,
    StreamBridgeTTSProvider,
    _wav_header,
    _wav_pcm_data,
    build_asr_provider,
    build_tts_provider,
)

_AUDIO_ENVS = (
    "ASR_PROVIDER", "TTS_PROVIDER", "LLM_PROVIDER", "LLM_API_KEY",
    "DASHSCOPE_ASR_KEY", "LLM_EMBED_API_KEY", "MINIMAX_API_KEY",
    "TTS_STREAM_PROVIDER", "MIMO_AUDIO_BASE_URL",
    "ASR_STREAM_PROVIDER", "ASR_STREAM_MODEL", "TTS_STREAM_MODEL",
    "REQUIRE_REAL_PROVIDERS",
)


def _clean_env(monkeypatch):
    for k in _AUDIO_ENVS:
        monkeypatch.delenv(k, raising=False)


# ── 工厂：ASR ──────────────────────────────────────────────────────────

def test_asr_default_mimo_unchanged(monkeypatch):
    """历史现状不变：LLM_PROVIDER=mimo 系 + 有 key → MiMo 批处理。"""
    _clean_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "xiaomimimo")
    monkeypatch.setenv("LLM_API_KEY", "mk")
    assert isinstance(build_asr_provider(), MiMoASRProvider)


def test_asr_chat_switched_bridges_dashscope(monkeypatch):
    """chat 换家 + 有百炼 key → 桥接 dashscope 流式引擎（此前会静默 Mock）。"""
    _clean_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("LLM_API_KEY", "dsk")
    monkeypatch.setenv("LLM_EMBED_API_KEY", "bailian")
    prov = build_asr_provider()
    assert isinstance(prov, StreamBridgeASRProvider) and prov.provider == "dashscope"


def test_asr_chat_switched_no_key_mock(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("LLM_API_KEY", "dsk")
    assert isinstance(build_asr_provider(), MockASRProvider)


def test_asr_explicit_mimo_pins_despite_chat_switch(monkeypatch):
    """显式 ASR_PROVIDER=mimo：chat 切走后批处理仍钉住 MiMo。"""
    _clean_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("LLM_API_KEY", "mk")
    monkeypatch.setenv("ASR_PROVIDER", "mimo")
    assert isinstance(build_asr_provider(), MiMoASRProvider)


def test_asr_explicit_mock_wins(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "xiaomimimo")
    monkeypatch.setenv("LLM_API_KEY", "mk")
    monkeypatch.setenv("ASR_PROVIDER", "mock")
    assert isinstance(build_asr_provider(), MockASRProvider)


# ── 工厂：TTS ──────────────────────────────────────────────────────────

def test_tts_default_mimo_unchanged(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "xiaomimimo")
    monkeypatch.setenv("LLM_API_KEY", "mk")
    assert isinstance(build_tts_provider(), MiMoTTSProvider)


def test_tts_chat_switched_bridges_stream_engine(monkeypatch):
    """chat 换家 → 批处理 TTS 跟随 TTS_STREAM_PROVIDER（默认 cosyvoice）桥接。"""
    _clean_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("LLM_API_KEY", "dsk")
    monkeypatch.setenv("LLM_EMBED_API_KEY", "bailian")
    prov = build_tts_provider()
    assert isinstance(prov, StreamBridgeTTSProvider) and prov.engine == "cosyvoice"


def test_tts_explicit_minimax(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("TTS_PROVIDER", "minimax")
    monkeypatch.setenv("MINIMAX_API_KEY", "mmk")
    prov = build_tts_provider()
    assert isinstance(prov, StreamBridgeTTSProvider) and prov.engine == "minimax"


def test_tts_explicit_engine_without_key_mock(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("TTS_PROVIDER", "minimax")
    assert isinstance(build_tts_provider(), MockTTSProvider)


def test_tts_request_pin_can_override_the_process_default(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "xiaomimimo")
    monkeypatch.setenv("LLM_API_KEY", "mimo-key")
    monkeypatch.setenv("LLM_EMBED_API_KEY", "bailian-key")

    prov = build_tts_provider("cosyvoice")

    assert isinstance(prov, StreamBridgeTTSProvider)
    assert prov.engine == "cosyvoice"


def test_tts_explicit_mimo_pins(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("LLM_API_KEY", "mk")
    monkeypatch.setenv("TTS_PROVIDER", "mimo")
    assert isinstance(build_tts_provider(), MiMoTTSProvider)


def test_batch_tts_providers_expose_the_real_provider_identity():
    assert MockTTSProvider().provider == "mock"
    assert MiMoTTSProvider("key").provider == "mimo"
    assert StreamBridgeTTSProvider("cosyvoice").provider == "cosyvoice"


# ── MiMo 端点可配 ──────────────────────────────────────────────────────

def test_mimo_audio_base_url_override(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("MIMO_AUDIO_BASE_URL", "https://alt.example.com/v1/chat/completions")
    assert MiMoASRProvider("k").base_url == "https://alt.example.com/v1/chat/completions"
    assert MiMoTTSProvider("k").base_url == "https://alt.example.com/v1/chat/completions"
    # 缺省回落官方集群
    monkeypatch.delenv("MIMO_AUDIO_BASE_URL", raising=False)
    assert MiMoTTSProvider("k").base_url == MiMoTTSProvider.BASE_URL


# ── WAV data 块提取 ────────────────────────────────────────────────────

def test_wav_pcm_data_standard_and_raw():
    pcm = b"\x01\x02" * 100
    assert _wav_pcm_data(_wav_header(len(pcm)) + pcm) == pcm
    assert _wav_pcm_data(pcm) == pcm  # 非 RIFF 视为裸 PCM


def test_wav_pcm_data_streaming_placeholder_size():
    """ffmpeg pipe 产物：data size 可能是 0xFFFFFFFF 占位 → 取到末尾。"""
    pcm = b"\xaa\xbb" * 50
    hdr = bytearray(_wav_header(len(pcm)))
    hdr[-4:] = (0xFFFFFFFF).to_bytes(4, "little")
    assert _wav_pcm_data(bytes(hdr) + pcm) == pcm


# ── 流式桥接：TTS ──────────────────────────────────────────────────────

class _FakeStreamTTS:
    def __init__(self, sr=22050):
        self.sr = sr
        self.seen_voice = None

    async def stream(self, text_deltas, *, voice="", sample_rate=0):
        self.seen_voice = voice
        async for _ in text_deltas:
            pass
        yield {"type": "meta", "sample_rate": self.sr, "format": "pcm"}
        yield b"\x00\x01" * 800
        yield b"\x02\x03" * 800


def test_bridge_tts_synthesize_wav(monkeypatch):
    fake = _FakeStreamTTS(sr=22050)
    monkeypatch.setattr(P, "build_tts_stream_provider", lambda *a, **k: fake)
    bridge = StreamBridgeTTSProvider("cosyvoice")
    audio, fmt, dur, model, voice = asyncio.run(
        bridge.synthesize("你好世界", voice_id="冰糖", model="mimo-v2.5-tts",
                          speed=1.0, fmt="wav"))
    assert fmt == "wav" and audio[:4] == b"RIFF"
    assert _wav_pcm_data(audio) == b"\x00\x01" * 800 + b"\x02\x03" * 800
    # MiMo 音色「冰糖」不属于 cosyvoice → 不透传（引擎用自己默认），避免跨引擎 4xx
    assert fake.seen_voice == ""
    assert model == "cosyvoice-v3-flash"
    assert voice == "longxiaochun_v3"
    assert dur == int(3200 / (22050 * 2) * 1000)


def test_bridge_tts_known_voice_passthrough(monkeypatch):
    fake = _FakeStreamTTS()
    monkeypatch.setattr(P, "build_tts_stream_provider", lambda *a, **k: fake)
    bridge = StreamBridgeTTSProvider("cosyvoice")
    asyncio.run(bridge.synthesize("hi", voice_id="longze_v3", model="",
                                  speed=1.0, fmt="pcm16"))
    assert fake.seen_voice == "longze_v3"


def test_bridge_tts_list_voices():
    bridge = StreamBridgeTTSProvider("qwen")
    voices = asyncio.run(bridge.list_voices(language="zh", gender="male"))
    assert voices and all(v["gender"] == "male" for v in voices)


# ── 流式桥接：ASR ──────────────────────────────────────────────────────

class _FakeStreamASR:
    model = "fake-rt-model"

    def __init__(self):
        self.frames = []

    async def stream(self, pcm_chunks, *, language="zh"):
        async for c in pcm_chunks:
            self.frames.append(c)
        yield {"text": "拿起", "final": False}
        yield {"text": "拿起红色方块", "final": True}


def test_bridge_asr_transcribe(monkeypatch):
    fake = _FakeStreamASR()
    monkeypatch.setattr(P, "build_streaming_asr_provider", lambda *a, **k: fake)
    bridge = StreamBridgeASRProvider("dashscope")
    pcm = b"\x00\x01" * 16000  # 1s @16k s16le
    wav = _wav_header(len(pcm)) + pcm
    text, conf, lang, model, dur = asyncio.run(
        bridge.transcribe(audio=wav, fmt="wav", language="zh", model="mimo-v2.5-asr"))
    assert text == "拿起红色方块"
    assert model == "fake-rt-model"
    assert dur == 1000
    assert b"".join(fake.frames) == pcm  # WAV 头被剥掉、裸 PCM 完整喂入


def test_bridge_asr_no_engine_raises(monkeypatch):
    monkeypatch.setattr(P, "build_streaming_asr_provider", lambda *a, **k: None)
    bridge = StreamBridgeASRProvider("dashscope")
    try:
        asyncio.run(bridge.transcribe(audio=b"", fmt="wav", language="zh", model=""))
        assert False, "should raise"
    except RuntimeError:
        pass


# ── Mock 契约（无 key 控制台演示的兜底，必须离线确定性）────────────────

def test_mock_asr_offline_contract():
    prov = MockASRProvider()
    text, conf, lang, model, dur = asyncio.run(
        prov.transcribe(audio=b"\x00" * 320, fmt="wav", language="", model=""))
    assert text.startswith("[mock ASR]") and text  # 固定文本，非空
    assert conf == 0.0 and lang == "zh" and model == "mock" and dur == 0
    # 语言透传 + 再次调用结果一致（确定性）
    again = asyncio.run(prov.transcribe(audio=b"", fmt="pcm16", language="en", model="m"))
    assert again[0] == text and again[2] == "en"


def test_mock_tts_offline_contract():
    prov = MockTTSProvider()
    audio, fmt, dur, model, voice = asyncio.run(
        prov.synthesize("你好", voice_id="", model="", speed=1.0, fmt=""))
    assert audio == b"" and fmt == "wav" and dur == 0
    assert model == "mock" and voice == "mimo_default"
    voices = asyncio.run(prov.list_voices(language="", gender=""))
    assert voices and voices[0]["voice_id"] == "mock_voice"
