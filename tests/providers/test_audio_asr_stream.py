# New tests for the ported audio module (no upstream counterpart: car-agent only exercised the
# streaming ASR engines via live e2e scripts in test/). Covers build_streaming_asr_provider
# dispatch (env-scrubbed, offline), vad_silence_ms clamping, _wav_header layout,
# MiMoChunkedASRProvider pseudo-partial loop with a fake batch engine, and request-shape checks
# of the two DashScope ws protocols against scripted FakeWS sessions. NO network involved.
"""流式 ASR 单测：工厂路由 + 分块回退引擎 + DashScope 协议请求形状（FakeWS，离线）。"""
from __future__ import annotations

import asyncio
import base64
import json
import struct
import types

import pytest

from embodied.providers.audio import (
    DashScopeInferenceASRProvider,
    DashScopeRealtimeASRProvider,
    MiMoASRProvider,
    MiMoChunkedASRProvider,
    _wav_header,
    build_streaming_asr_provider,
)

_ASR_ENVS = (
    "ASR_STREAM_PROVIDER", "ASR_STREAM_MODEL", "ASR_MODEL", "DASHSCOPE_ASR_KEY",
    "LLM_EMBED_API_KEY", "LLM_API_KEY", "DASHSCOPE_ASR_WS_URL",
    "DASHSCOPE_ASR_INFERENCE_WS_URL",
)


def _clean_env(monkeypatch):
    for k in _ASR_ENVS:
        monkeypatch.delenv(k, raising=False)


async def _aiter(items):
    for x in items:
        yield x


# ── WAV 头（44 字节，16k mono s16le 缺省）────────────────────────────────

def test_wav_header_layout():
    hdr = _wav_header(3200)
    assert len(hdr) == 44
    assert hdr[:4] == b"RIFF" and hdr[8:16] == b"WAVEfmt "
    fmt_size, audio_fmt, channels, sr, byte_rate, block_align, bits = struct.unpack(
        "<IHHIIHH", hdr[16:36])
    assert (fmt_size, audio_fmt, channels, bits) == (16, 1, 1, 16)
    assert sr == 16000 and byte_rate == 32000 and block_align == 2
    assert hdr[36:40] == b"data" and struct.unpack("<I", hdr[40:44])[0] == 3200
    assert struct.unpack("<I", hdr[4:8])[0] == 36 + 3200
    # 采样率参数生效
    assert struct.unpack("<I", _wav_header(0, sr=22050)[24:28])[0] == 22050


# ── 工厂路由 ────────────────────────────────────────────────────────────

def test_stream_asr_factory_off_and_unknown(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("ASR_STREAM_PROVIDER", "off")
    assert build_streaming_asr_provider() is None
    assert build_streaming_asr_provider("none") is None
    assert build_streaming_asr_provider("whisperx") is None  # 未知引擎 → None


def test_stream_asr_factory_dashscope_needs_key(monkeypatch):
    _clean_env(monkeypatch)
    assert build_streaming_asr_provider("dashscope") is None


def test_stream_asr_factory_qwen_realtime_default(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("DASHSCOPE_ASR_KEY", "sk-test")
    prov = build_streaming_asr_provider("dashscope")
    assert isinstance(prov, DashScopeRealtimeASRProvider)
    assert prov.model == "qwen3-asr-flash-realtime-2026-02-10"
    assert prov.ws_url == "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
    assert prov.vad_silence_ms == 800  # 缺省


def test_stream_asr_factory_key_fallback_to_embed_key(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("LLM_EMBED_API_KEY", "bailian")
    prov = build_streaming_asr_provider("dashscope")
    assert isinstance(prov, DashScopeRealtimeASRProvider) and prov.api_key == "bailian"


def test_stream_asr_factory_fun_alias_uses_inference_protocol(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("DASHSCOPE_ASR_KEY", "sk-test")
    prov = build_streaming_asr_provider("fun")
    assert isinstance(prov, DashScopeInferenceASRProvider)
    assert prov.model == "fun-asr-realtime"
    assert prov.ws_url == "wss://dashscope.aliyuncs.com/api-ws/v1/inference"


def test_stream_asr_factory_non_qwen_model_routes_to_inference(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("DASHSCOPE_ASR_KEY", "sk-test")
    prov = build_streaming_asr_provider("dashscope", model="paraformer-realtime-v2")
    assert isinstance(prov, DashScopeInferenceASRProvider)
    assert prov.model == "paraformer-realtime-v2"


def test_stream_asr_factory_vad_silence_clamped(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("DASHSCOPE_ASR_KEY", "sk-test")
    assert build_streaming_asr_provider("qwen3", vad_silence_ms=100).vad_silence_ms == 300
    assert build_streaming_asr_provider("qwen3", vad_silence_ms=5000).vad_silence_ms == 2000
    assert build_streaming_asr_provider("qwen3", vad_silence_ms=0).vad_silence_ms == 800
    # 构造入参异常 → 回落 800（现状）
    assert DashScopeRealtimeASRProvider("k", "wss://x", "m", vad_silence_ms="abc").vad_silence_ms == 800


def test_stream_asr_factory_mimo_chunked(monkeypatch):
    _clean_env(monkeypatch)
    assert build_streaming_asr_provider("mimo") is None  # 无 LLM_API_KEY → None
    monkeypatch.setenv("LLM_API_KEY", "mk")
    prov = build_streaming_asr_provider("mimo-chunked")
    assert isinstance(prov, MiMoChunkedASRProvider)
    assert isinstance(prov.batch, MiMoASRProvider)
    assert prov.model == "mimo-v2.5-asr"  # ASR_MODEL 缺省


# ── MiMo 分块回退引擎（伪 partial → 定稿），fake 批处理离线驱动 ──────────

class _FakeBatchASR:
    def __init__(self):
        self.calls = []

    async def transcribe(self, audio, fmt, language, model):
        self.calls.append((audio, fmt, language, model))
        return f"text-{len(self.calls)}", 0.9, language, model, 100


@pytest.mark.asyncio
async def test_mimo_chunked_partial_then_final(monkeypatch):
    monkeypatch.delenv("ASR_MODEL", raising=False)
    fake = _FakeBatchASR()
    prov = MiMoChunkedASRProvider(fake, model="mimo-v2.5-asr")
    out = [ev async for ev in prov.stream(_aiter([b"\x01\x02" * 2000]), language="zh")]
    assert out == [{"text": "text-1", "final": False}, {"text": "text-2", "final": True}]
    assert len(fake.calls) == 2
    audio, fmt, lang, model = fake.calls[0]
    assert audio[:4] == b"RIFF" and fmt == "wav"  # 裸 PCM 封了 WAV 头再打批处理
    assert lang == "zh" and model == "mimo-v2.5-asr"


@pytest.mark.asyncio
async def test_mimo_chunked_tiny_buffer_skips_batch():
    fake = _FakeBatchASR()
    prov = MiMoChunkedASRProvider(fake, model="m")
    out = [ev async for ev in prov.stream(_aiter([b"\x00" * 100]))]  # <3200B 不值得打
    assert out == [{"text": "", "final": True}]
    assert fake.calls == []


# ── FakeWS：DashScope 两种协议的请求形状 + partial/final 聚合（离线）──────

class _FakeMsg(types.SimpleNamespace):
    pass


class _FakeWS:
    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.sent = []
        self.close_code = None

    async def send_json(self, obj):
        self.sent.append(obj)

    async def send_bytes(self, b):
        self.sent.append(b)

    async def receive(self, timeout=None):
        await asyncio.sleep(0)  # 让 pump 任务有机会发帧
        if self._scripted:
            return self._scripted.pop(0)
        import aiohttp
        return _FakeMsg(type=aiohttp.WSMsgType.CLOSED, data=None)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeSession:
    def __init__(self, ws):
        self._ws = ws

    def ws_connect(self, *a, **k):
        return self._ws

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _text(obj):
    import aiohttp
    return _FakeMsg(type=aiohttp.WSMsgType.TEXT, data=json.dumps(obj))


@pytest.mark.asyncio
async def test_realtime_asr_request_shape_and_events(monkeypatch):
    import aiohttp
    ws = _FakeWS([
        _text({"type": "session.created"}),
        _text({"type": "conversation.item.input_audio_transcription.text",
               "text": "拿起", "stash": "红"}),
        _text({"type": "conversation.item.input_audio_transcription.completed",
               "transcript": "拿起红色方块"}),
    ])
    monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **k: _FakeSession(ws))
    prov = DashScopeRealtimeASRProvider("sk", "wss://x/realtime", "qwen3-asr-flash-realtime",
                                        vad_silence_ms=1000)
    chunk = b"\x01\x02" * 1600  # 100ms @16k s16le
    out = [ev async for ev in prov.stream(_aiter([chunk]), language="zh")]
    assert out == [{"text": "拿起红", "final": False},
                   {"text": "拿起红色方块", "final": True}]
    # 请求形状：session.update 先行，format=pcm/16k + server_vad 透传客户端静音尾
    upd = ws.sent[0]
    assert upd["type"] == "session.update"
    assert upd["session"]["input_audio_format"] == "pcm" and upd["session"]["sample_rate"] == 16000
    assert upd["session"]["input_audio_transcription"] == {"language": "zh"}
    assert upd["session"]["turn_detection"]["type"] == "server_vad"
    assert upd["session"]["turn_detection"]["silence_duration_ms"] == 1000
    appends = [f for f in ws.sent if isinstance(f, dict) and f.get("type") == "input_audio_buffer.append"]
    assert appends and base64.b64decode(appends[0]["audio"]) == chunk  # base64 音频帧
    for extra in appends[1:]:  # 其后只有流末兜底静音帧
        assert base64.b64decode(extra["audio"]) == b"\x00" * 3200


@pytest.mark.asyncio
async def test_realtime_asr_no_transcript_raises(monkeypatch):
    import aiohttp
    ws = _FakeWS([_text({"type": "session.created"})])  # 之后直接 CLOSED，无转写
    monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **k: _FakeSession(ws))
    prov = DashScopeRealtimeASRProvider("sk", "wss://x/realtime", "qwen3-asr")
    with pytest.raises(RuntimeError, match="无转写"):
        _ = [ev async for ev in prov.stream(_aiter([b"\x00" * 3200]))]


@pytest.mark.asyncio
async def test_inference_asr_request_shape_and_sentence_accumulation(monkeypatch):
    import aiohttp
    ws = _FakeWS([
        _text({"header": {"event": "task-started"}}),
        _text({"header": {"event": "result-generated"},
               "payload": {"output": {"sentence": {"text": "拿起", "sentence_end": False}}}}),
        _text({"header": {"event": "result-generated"},
               "payload": {"output": {"sentence": {"text": "拿起红色方块", "sentence_end": True}}}}),
        _text({"header": {"event": "task-finished"}}),
    ])
    monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **k: _FakeSession(ws))
    prov = DashScopeInferenceASRProvider("sk", "wss://x/inference", "fun-asr-realtime")
    chunk = b"\x03\x04" * 1600
    out = [ev async for ev in prov.stream(_aiter([chunk]))]
    assert out[0] == {"text": "拿起", "final": False}
    assert out[-1] == {"text": "拿起红色方块", "final": True}
    # 请求形状：run-task 先行（recognition/pcm/16k），音频走二进制帧，末尾 finish-task
    run = ws.sent[0]
    assert run["header"]["action"] == "run-task" and run["header"]["streaming"] == "duplex"
    assert run["payload"]["task_group"] == "audio" and run["payload"]["task"] == "asr"
    assert run["payload"]["function"] == "recognition"
    assert run["payload"]["model"] == "fun-asr-realtime"
    assert run["payload"]["parameters"] == {"format": "pcm", "sample_rate": 16000}
    assert chunk in ws.sent  # 二进制音频帧（非 base64）
    finishes = [f for f in ws.sent
                if isinstance(f, dict) and f.get("header", {}).get("action") == "finish-task"]
    assert finishes


@pytest.mark.asyncio
async def test_inference_asr_task_failed_raises(monkeypatch):
    import aiohttp
    ws = _FakeWS([
        _text({"header": {"event": "task-started"}}),
        _text({"header": {"event": "task-failed", "error_message": "InvalidParameter"}}),
    ])
    monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **k: _FakeSession(ws))
    prov = DashScopeInferenceASRProvider("sk", "wss://x/inference", "fun-asr-realtime")
    with pytest.raises(RuntimeError, match="InvalidParameter"):
        _ = [ev async for ev in prov.stream(_aiter([b"\x00" * 3200]))]
