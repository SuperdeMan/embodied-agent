# Ported from car-agent llm-gateway/providers.py (audio section) @ f0b08f8, changes: extracted
# lines 556-1722 into a standalone module (module docstring + import block added; section lived
# mid-file upstream); _HTTP_LIMITS/_HTTP_CONNECT_S/_strict_mock_gate imported from
# embodied.providers.llm instead of duplicated; function-level import blocks isort-formatted and
# one malformed noqa comment rewritten (ruff). Provider logic, signatures, protocol frames
# and env var names unchanged. Upstream doc paths (docs/design/..., http_server.py) refer to
# car-agent.
"""ASR/TTS provider abstractions and implementations (batch + streaming).

Same env-first philosophy as llm.py: switching audio vendors is an env change, not a
code change. Factories: build_asr_provider / build_streaming_asr_provider (speech to
text), build_tts_provider / build_tts_stream_provider (text to speech). Mock providers
keep the console loop runnable offline without any API key.
"""
from __future__ import annotations

import asyncio
import json
import os
import re

import httpx

from .llm import _HTTP_CONNECT_S, _HTTP_LIMITS, _strict_mock_gate

# ─── ASR Provider（语音识别）───
# 官方文档：https://platform.xiaomimimo.com/docs/zh-CN/api/audio/Speech-Recognition

class BaseASRProvider:
    async def transcribe(self, audio: bytes, fmt: str, language: str, model: str):
        """returns (text, confidence, language, model_used, duration_ms)"""
        raise NotImplementedError


class MockASRProvider(BaseASRProvider):
    """无 API key 时的 ASR 兜底。"""
    async def transcribe(self, audio: bytes, fmt: str, language: str, model: str):
        return "[mock ASR] 语音识别结果（配置 LLM_API_KEY 后接入真实 ASR）", 0.0, language or "zh", "mock", 0


class MiMoASRProvider(BaseASRProvider):
    """小米 MiMo ASR API。

    与 LLM 共用同一 endpoint（/v1/chat/completions），音频通过 base64 data URI 传入。
    响应格式为 OpenAI chat.completion，识别文本在 choices[0].message.content。
    """
    BASE_URL = "https://token-plan-cn.xiaomimimo.com/v1/chat/completions"

    def __init__(self, api_key: str, base_url: str = ""):
        self.api_key = api_key
        # MiMo 音频端点可配（MIMO_AUDIO_BASE_URL，ASR/TTS 共用），与 chat 的 LLM_BASE_URL 独立
        self.base_url = base_url or os.getenv("MIMO_AUDIO_BASE_URL", "") or self.BASE_URL
        self._client: httpx.AsyncClient | None = None

    async def transcribe(self, audio: bytes, fmt: str, language: str, model: str):
        import base64

        # 音频编码为 base64 data URI
        mime = f"audio/{fmt or 'wav'}"
        b64 = base64.b64encode(audio).decode("ascii")
        data_uri = f"data:{mime};base64,{b64}"

        headers = {"api-key": self.api_key, "Content-Type": "application/json"}
        body = {
            "model": model or os.getenv("ASR_MODEL", "mimo-v2.5-asr"),
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_audio", "input_audio": {"data": data_uri}},
                    ],
                }
            ],
            "asr_options": {"language": language or "auto"},
        }

        if self._client is None:
            self._client = httpx.AsyncClient(limits=_HTTP_LIMITS)
        resp = await self._client.post(self.base_url, headers=headers, json=body, timeout=60)
        resp.raise_for_status()
        result = resp.json()

        # 响应：choices[0].message.content = 识别文本，usage.seconds = 音频秒数
        text = result["choices"][0]["message"]["content"]
        duration_sec = result.get("usage", {}).get("seconds", 0)
        return text, 0.9, language or "zh", model or "mimo-v2.5-asr", int(duration_sec * 1000)


def _wav_pcm_data(audio: bytes) -> bytes:
    """提取 WAV 的 data 块裸 PCM；非 RIFF 输入视为已是裸 PCM 原样返回。
    容忍 ffmpeg pipe 产物（RIFF/data 的 size 字段可能是 0 或 0xFFFFFFFF 占位）。"""
    if len(audio) < 12 or audio[:4] != b"RIFF":
        return audio
    i = audio.find(b"data", 12)
    if i < 0 or i + 8 > len(audio):
        return audio
    size = int.from_bytes(audio[i + 4:i + 8], "little")
    start = i + 8
    if size in (0, 0xFFFFFFFF) or start + size > len(audio):
        return audio[start:]
    return audio[start:start + size]


class StreamBridgeASRProvider(BaseASRProvider):
    """把流式 ASR 引擎适配成批处理接口（/api/asr + gRPC Transcribe）。

    存在意义：批处理面此前硬绑 MiMo——chat 换家（LLM_PROVIDER≠mimo 系）即静默降级
    Mock。经此桥接，批处理可跟随 dashscope 等流式引擎：WAV→裸 PCM→按帧喂流式引擎→
    取定稿文本。model 参数忽略（引擎模型由 ASR_STREAM_MODEL 控制）。
    """

    def __init__(self, provider: str):
        self.provider = provider

    async def transcribe(self, audio: bytes, fmt: str, language: str, model: str):
        engine = build_streaming_asr_provider(self.provider)
        if engine is None:
            raise RuntimeError(f"ASR 引擎 {self.provider} 无可用 key")
        pcm = _wav_pcm_data(audio)
        duration_ms = int(len(pcm) / 32000 * 1000)  # 16kHz mono s16le = 32000 B/s

        async def frames():
            step = 3200  # 100ms @16k s16le
            for i in range(0, len(pcm), step):
                yield pcm[i:i + step]

        text = ""
        async for ev in engine.stream(frames(), language=language or "zh"):
            if ev.get("text"):
                text = ev["text"]
        model_used = getattr(engine, "model", "") or self.provider
        return text, 0.9, language or "zh", model_used, duration_ms


def build_asr_provider() -> BaseASRProvider:
    """批处理 ASR 工厂（/api/asr + gRPC Transcribe 共用，启动时装配）。
    ASR_PROVIDER：auto（默认）| mimo | dashscope | mock。
    auto：LLM_PROVIDER 为 MiMo 系且有 LLM_API_KEY → MiMo（历史现状）；否则有
    dashscope key → 桥接流式引擎；都不可用 → Mock。显式 mimo 复用 LLM_API_KEY
    （多 LLM 源惯例：该 env 即 MiMo 的 key），chat 切走后批处理仍可钉住 MiMo。"""
    choice = os.getenv("ASR_PROVIDER", "auto").strip().lower()
    api_key = os.getenv("LLM_API_KEY", "")

    def _mock(why: str):
        _strict_mock_gate("asr", why)
        return MockASRProvider()

    if choice == "mock":
        return _mock("ASR_PROVIDER=mock 显式指定")
    if choice in ("mimo", "xiaomimimo"):
        return MiMoASRProvider(api_key) if api_key else _mock("mimo 引擎但无 LLM_API_KEY")
    llm_provider = os.getenv("LLM_PROVIDER", "xiaomimimo").lower()
    if choice == "auto" and llm_provider in ("xiaomimimo", "mimo") and api_key:
        return MiMoASRProvider(api_key)
    if choice in ("auto", "dashscope") and build_streaming_asr_provider("dashscope") is not None:
        return StreamBridgeASRProvider("dashscope")
    return _mock("无可用引擎（MiMo/DashScope key 均缺）")


# ─── 流式 ASR Provider（实时识别上屏）───
# 设计见 docs/design/2026-06-30-asr-streaming-design.md。
# 传输层（HMI↔网关 WS + 网关流式 ffmpeg）在 http_server.py；此处是
# "16k mono PCM16 帧流 → {text, final} 文本流" 的引擎抽象，引擎经 env/请求可换。

def _wav_header(pcm_len: int, sr: int = 16000) -> bytes:
    """给裸 PCM16 mono 加 44 字节 WAV 头（供 MiMo 批 ASR 当 wav 用）。"""
    import struct
    return (b"RIFF" + struct.pack("<I", 36 + pcm_len) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, sr, sr * 2, 2, 16)
            + b"data" + struct.pack("<I", pcm_len))


class BaseStreamingASRProvider:
    async def stream(self, pcm_chunks, *, language: str = "zh"):
        """pcm_chunks: 16kHz mono s16le PCM 帧的异步迭代器；yield {'text': str, 'final': bool}。"""
        raise NotImplementedError
        yield  # marks this as an async generator (never reached)


class MiMoChunkedASRProvider(BaseStreamingASRProvider):
    """回退引擎：累积 PCM、每 ~interval 秒封 WAV 打一次 MiMo 批 ASR 产伪 partial。
    用已验证可用的 MiMo 批 ASR，保证上屏功能在 DashScope 不可用时也跑通。"""

    def __init__(self, batch: "MiMoASRProvider", model: str = "", interval_s: float = 1.2):
        self.batch = batch
        self.model = model or os.getenv("ASR_MODEL", "mimo-v2.5-asr")
        self.interval_s = interval_s

    async def stream(self, pcm_chunks, *, language="zh"):
        import time
        buf = bytearray()
        last_t = 0.0
        last_text = ""

        async def transcribe_now() -> str:
            if len(buf) < 3200:  # <0.1s 不值得打
                return last_text
            wav = _wav_header(len(buf)) + bytes(buf)
            text, *_ = await self.batch.transcribe(audio=wav, fmt="wav", language=language, model=self.model)
            return (text or "").strip()

        async for chunk in pcm_chunks:
            buf.extend(chunk)
            now = time.monotonic()
            if now - last_t >= self.interval_s:
                last_t = now
                try:
                    t = await transcribe_now()
                    if t and t != last_text:
                        last_text = t
                        yield {"text": t, "final": False}
                except Exception:
                    pass  # 中途失败不影响整段定稿
        try:
            final = await transcribe_now()
        except Exception:
            final = last_text
        yield {"text": (final or last_text), "final": True}


class DashScopeRealtimeASRProvider(BaseStreamingASRProvider):
    """DashScope（百炼）实时 ASR——OpenAI 兼容 Realtime 协议（实测见设计 §2.1）。
    端点 wss://…/api-ws/v1/realtime?model=<id>，Bearer 鉴权。
    session.created → session.update → input_audio_buffer.append(base64 PCM16) →
    （流末）commit → conversation.item.input_audio_transcription.delta(partial)/.completed(final)。"""

    def __init__(self, api_key: str, ws_url: str, model: str, vad_silence_ms: int = 800):
        self.api_key = api_key
        self.ws_url = ws_url
        self.model = model
        # R4.3b P2（U5b 治本）：server_vad 静音尾由客户端透传（原硬编码 800ms 盖过客户端设置），
        # 夹紧 [300, 2000]，异常/缺省回落 800（现状）。fun-asr 走客户端 stop 端点，不受此影响。
        try:
            self.vad_silence_ms = int(vad_silence_ms) if vad_silence_ms else 800
        except (TypeError, ValueError):
            self.vad_silence_ms = 800
        self.vad_silence_ms = max(300, min(2000, self.vad_silence_ms))

    async def stream(self, pcm_chunks, *, language="zh"):
        import base64

        import aiohttp
        url = f"{self.ws_url}?model={self.model}"
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                url, headers={"Authorization": f"Bearer {self.api_key}"}, heartbeat=20.0,
            ) as ws:
                # 等 session.created（容错读几条）
                for _ in range(5):
                    msg = await ws.receive(timeout=10)
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        if json.loads(msg.data).get("type") == "session.created":
                            break
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSING):
                        raise RuntimeError("dashscope ws closed before session.created")
                _eid = [0]

                def _ev(o):
                    _eid[0] += 1
                    return {"event_id": f"ev{_eid[0]}", **o}

                # 实测协议：format=pcm（非 pcm16）、transcription.language（非 model）、**server_vad**
                # （turn_detection=None 手动模式在该模型上报 1011，server_vad 实测可用）；
                # 中间结果 .text(text+stash)、定稿 .completed(transcript)。
                await ws.send_json(_ev({"type": "session.update", "session": {
                    "input_audio_format": "pcm", "sample_rate": 16000,
                    "input_audio_transcription": {"language": language or "zh"},
                    "turn_detection": {"type": "server_vad", "threshold": 0.2,
                                       "silence_duration_ms": self.vad_silence_ms},
                }}))

                async def pump():
                    try:
                        async for chunk in pcm_chunks:
                            await ws.send_json(_ev({"type": "input_audio_buffer.append",
                                                    "audio": base64.b64encode(chunk).decode("ascii")}))
                        # 流末（松手）追静音触发 server_vad 收尾定稿——须 > silence_duration_ms 才生效，
                        # 故按 vad_silence_ms 放大帧数（每帧 100ms 静音）：silence 越长、兜底静音越长。
                        sil = base64.b64encode(b"\x00" * 3200).decode("ascii")  # 100ms @16k s16le
                        tail_frames = max(13, self.vad_silence_ms // 100 + 4)
                        for _ in range(tail_frames):
                            await ws.send_json(_ev({"type": "input_audio_buffer.append", "audio": sil}))
                            await asyncio.sleep(0.05)
                    except Exception:
                        pass

                pump_task = asyncio.create_task(pump())
                acc = ""
                final_sent = False
                try:
                    while True:
                        try:
                            msg = await ws.receive(timeout=15.0)
                        except asyncio.TimeoutError:
                            break  # 模型长时间无响应（如 fun-asr-realtime 不出转写）→ 退出，下面按无转写处理
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
                            continue
                        m = json.loads(msg.data)
                        t = m.get("type", "")
                        if "input_audio_transcription" in t and t.endswith(".text"):
                            acc = (m.get("text") or "") + (m.get("stash") or "")  # 已确认 + 草稿后缀
                            if acc:
                                yield {"text": acc, "final": False}
                        elif "input_audio_transcription" in t and t.endswith("completed"):
                            acc = (m.get("transcript") or acc)
                            yield {"text": acc, "final": True}
                            final_sent = True
                            break
                        elif t == "error":
                            raise RuntimeError((m.get("error") or {}).get("message", "dashscope asr error"))
                finally:
                    pump_task.cancel()
                if not final_sent:
                    # 异常关闭/无转写（如服务端 1011 InternalError）→ 抛错让网关回 error、
                    # HMI 无感回退批处理；有半截 partial 则当定稿用。
                    if not acc:
                        raise RuntimeError(f"dashscope 实时无转写 (close_code={ws.close_code})")
                    yield {"text": acc, "final": True}


class DashScopeInferenceASRProvider(BaseStreamingASRProvider):
    """DashScope 实时 ASR——Fun-ASR / Paraformer 系，**run-task 协议**（端点 `/api-ws/v1/inference`，
    与 qwen3 的 OpenAI-realtime 协议不同！实测见 docs/design §2.1）。
    run-task(task_group=audio/task=asr/function=recognition/parameters{format,sample_rate}/input{}) →
    task-started → **二进制音频帧** → result-generated(payload.output.sentence{text,sentence_end}) →
    finish-task → task-finished。"""

    def __init__(self, api_key: str, ws_url: str, model: str):
        self.api_key = api_key
        self.ws_url = ws_url
        self.model = model

    async def stream(self, pcm_chunks, *, language="zh"):
        import uuid

        import aiohttp
        task_id = uuid.uuid4().hex
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                self.ws_url, headers={"Authorization": f"bearer {self.api_key}"}, heartbeat=20.0,
            ) as ws:
                await ws.send_json({"header": {"action": "run-task", "task_id": task_id, "streaming": "duplex"},
                                    "payload": {"task_group": "audio", "task": "asr", "function": "recognition",
                                                "model": self.model,
                                                "parameters": {"format": "pcm", "sample_rate": 16000},
                                                "input": {}}})
                started = asyncio.Event()

                async def pump():
                    try:
                        await started.wait()
                        async for chunk in pcm_chunks:
                            await ws.send_bytes(chunk)  # 二进制音频帧（非 base64）
                        await ws.send_json({"header": {"action": "finish-task", "task_id": task_id,
                                                       "streaming": "duplex"}, "payload": {"input": {}}})
                    except Exception:
                        pass

                pump_task = asyncio.create_task(pump())
                finalized = ""  # 已定稿句子前缀（多句时累积），current 句的 text 接其后
                acc = ""
                final_sent = False
                try:
                    while True:
                        try:
                            msg = await ws.receive(timeout=15.0)
                        except asyncio.TimeoutError:
                            break
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
                            continue
                        m = json.loads(msg.data)
                        evt = m.get("header", {}).get("event", "")
                        if evt == "task-started":
                            started.set()
                        elif evt == "result-generated":
                            sent = (m.get("payload", {}).get("output", {}) or {}).get("sentence", {}) or {}
                            acc = finalized + (sent.get("text") or "")
                            if acc:
                                yield {"text": acc, "final": False}
                            if sent.get("sentence_end"):
                                finalized = acc
                        elif evt == "task-finished":
                            yield {"text": acc, "final": True}
                            final_sent = True
                            break
                        elif evt == "task-failed":
                            raise RuntimeError(m.get("header", {}).get("error_message", "dashscope asr failed"))
                finally:
                    pump_task.cancel()
                if not final_sent:
                    if not acc:
                        raise RuntimeError("dashscope inference 无转写")
                    yield {"text": acc, "final": True}


def build_streaming_asr_provider(provider: str = "", model: str = "",
                                 vad_silence_ms: int = 0) -> "BaseStreamingASRProvider | None":
    """按请求/env 选流式引擎。provider/model 为请求级覆盖（HMI 设置可切），空则用 env 默认。
    vad_silence_ms：客户端静音尾（R4.3b P2），仅 qwen3 realtime 的 server_vad 消费；0=用 provider 缺省。
    无可用 key 或 off → None（HMI 探测到则无感回退批处理 /api/asr）。"""
    provider = (provider or os.getenv("ASR_STREAM_PROVIDER", "dashscope")).strip().lower()
    if provider in ("", "off", "none"):
        return None
    if provider in ("dashscope", "dashscope-qwen3", "dashscope-fun", "qwen3", "fun"):
        key = os.getenv("DASHSCOPE_ASR_KEY") or os.getenv("LLM_EMBED_API_KEY", "")
        if not key:
            return None
        mdl = model or os.getenv("ASR_STREAM_MODEL", "qwen3-asr-flash-realtime-2026-02-10")
        if provider in ("fun", "dashscope-fun") and not model:
            mdl = "fun-asr-realtime"
        if "qwen" in mdl.lower():
            # Qwen-ASR：OpenAI 兼容 Realtime 协议（/realtime，base64 音频，session.update）
            ws_url = os.getenv("DASHSCOPE_ASR_WS_URL", "wss://dashscope.aliyuncs.com/api-ws/v1/realtime")
            return DashScopeRealtimeASRProvider(key, ws_url, mdl, vad_silence_ms=vad_silence_ms or 800)
        # Fun-ASR / Paraformer：run-task 协议（/inference，二进制音频帧）
        inf_url = os.getenv("DASHSCOPE_ASR_INFERENCE_WS_URL", "wss://dashscope.aliyuncs.com/api-ws/v1/inference")
        return DashScopeInferenceASRProvider(key, inf_url, mdl)
    if provider in ("mimo", "mimo-chunked", "mimo-batch"):
        api_key = os.getenv("LLM_API_KEY", "")
        if not api_key:
            return None
        return MiMoChunkedASRProvider(MiMoASRProvider(api_key), model=model)
    return None


# ─── TTS Provider（语音合成）───
# 官方文档：https://platform.xiaomimimo.com/docs/zh-CN/usage-guide/speech-synthesis-v2.5

class BaseTTSProvider:
    provider: str

    async def synthesize(self, text: str, voice_id: str, model: str,
                         speed: float, fmt: str):
        """returns (audio_bytes, format, duration_ms, model_used, voice_id)"""
        raise NotImplementedError

    async def list_voices(self, language: str, gender: str):
        """returns list of VoiceInfo dicts"""
        raise NotImplementedError


class MockTTSProvider(BaseTTSProvider):
    """无 API key 时的 TTS 兜底。"""
    provider = "mock"

    async def synthesize(self, text: str, voice_id: str, model: str,
                         speed: float, fmt: str):
        return b"", fmt or "wav", 0, "mock", voice_id or "mimo_default"

    async def list_voices(self, language: str, gender: str):
        return [
            {"voice_id": "mock_voice", "name": "模拟音色", "language": "zh",
             "gender": "female", "description": "Mock 音色（配置 LLM_API_KEY 后接入真实 TTS）", "tags": ["默认"]},
        ]


class MiMoTTSProvider(BaseTTSProvider):
    """小米 MiMo TTS API。

    与 LLM 共用同一 endpoint（/v1/chat/completions）。
    目标文本放在 role=assistant 的 content 中，可选 role=user 传风格控制。
    响应：choices[0].message.audio.data 为 Base64 编码音频（wav/pcm16）。
    """
    provider = "mimo"
    BASE_URL = "https://token-plan-cn.xiaomimimo.com/v1/chat/completions"

    # 官方预置音色（mimo-v2.5-tts）
    VOICES = [
        {"voice_id": "mimo_default", "name": "MiMo-默认", "language": "zh",
         "gender": "neutral", "description": "中国集群默认冰糖", "tags": ["默认"]},
        {"voice_id": "冰糖", "name": "冰糖", "language": "zh",
         "gender": "female", "description": "中文女声", "tags": ["中文", "女声"]},
        {"voice_id": "茉莉", "name": "茉莉", "language": "zh",
         "gender": "female", "description": "中文女声", "tags": ["中文", "女声"]},
        {"voice_id": "苏打", "name": "苏打", "language": "zh",
         "gender": "male", "description": "中文男声", "tags": ["中文", "男声"]},
        {"voice_id": "白桦", "name": "白桦", "language": "zh",
         "gender": "male", "description": "中文男声", "tags": ["中文", "男声"]},
        {"voice_id": "Mia", "name": "Mia", "language": "en",
         "gender": "female", "description": "英文女声", "tags": ["英文", "女声"]},
        {"voice_id": "Chloe", "name": "Chloe", "language": "en",
         "gender": "female", "description": "英文女声", "tags": ["英文", "女声"]},
        {"voice_id": "Milo", "name": "Milo", "language": "en",
         "gender": "male", "description": "英文男声", "tags": ["英文", "男声"]},
        {"voice_id": "Dean", "name": "Dean", "language": "en",
         "gender": "male", "description": "英文男声", "tags": ["英文", "男声"]},
    ]

    def __init__(self, api_key: str, base_url: str = ""):
        self.api_key = api_key
        # MiMo 音频端点可配（MIMO_AUDIO_BASE_URL，ASR/TTS 共用），与 chat 的 LLM_BASE_URL 独立
        self.base_url = base_url or os.getenv("MIMO_AUDIO_BASE_URL", "") or self.BASE_URL
        self._client: httpx.AsyncClient | None = None

    async def synthesize(self, text: str, voice_id: str, model: str,
                         speed: float, fmt: str):
        import base64

        headers = {
            "api-key": self.api_key,
            "Content-Type": "application/json",
            "Accept-Encoding": "identity",  # 禁用 gzip，避免解码问题
        }
        out_fmt = "pcm16" if fmt == "pcm16" else "wav"
        body = {
            "model": model or "mimo-v2.5-tts",
            "messages": [
                {"role": "assistant", "content": text},
            ],
            "audio": {
                "format": out_fmt,
                "voice": voice_id or "mimo_default",
            },
        }
        # speed 参数：通过 role=user 的风格指令传入（如"语速稍快"）
        # 当前不注入 speed 到请求体，保持简洁；需要时可加 role=user content

        if self._client is None:
            self._client = httpx.AsyncClient(limits=_HTTP_LIMITS)
        resp = await self._client.post(self.base_url, headers=headers, json=body, timeout=60)
        resp.raise_for_status()
        # MiMo TTS 返回 JSON（含 base64 音频），手动解析避免编码问题
        raw = resp.content  # 原始字节（已自动解压 gzip）
        try:
            import json as _json
            result = _json.loads(raw)
            audio_b64 = result["choices"][0]["message"]["audio"]["data"]
            audio_bytes = base64.b64decode(audio_b64)
        except (ValueError, KeyError, IndexError, UnicodeDecodeError):
            # fallback：响应体直接是音频字节流
            audio_bytes = raw
        # 估算时长：PCM16 24kHz mono = 48000 bytes/sec
        bytes_per_sec = 48000 if out_fmt == "pcm16" else 48000  # WAV 头忽略
        duration_ms = int(len(audio_bytes) / bytes_per_sec * 1000) if audio_bytes else 0
        return audio_bytes, out_fmt, duration_ms, model or "mimo-v2.5-tts", voice_id or "mimo_default"

    async def list_voices(self, language: str, gender: str):
        voices = self.VOICES
        if language:
            lang_short = language[:2].lower()
            voices = [v for v in voices if v["language"] == lang_short or v["language"] == "neutral"]
        if gender:
            voices = [v for v in voices if v["gender"] == gender]
        return voices


class StreamBridgeTTSProvider(BaseTTSProvider):
    """把流式 TTS 引擎（TTS_STREAM_CATALOG 各家）适配成批处理接口（/api/tts + gRPC Synthesize）。

    整段文本一次流入 → 聚齐 PCM → 封 WAV 返回。调用方传入的 model/voice 若不属于该
    引擎（如 HMI 旧默认「冰糖」打到 cosyvoice），忽略并用引擎目录默认，避免跨引擎
    音色 4xx（与 HMI settings 的同名回落逻辑对称，双侧防御）。
    """

    def __init__(self, engine: str):
        self.engine = engine
        self.provider = engine
        cat = TTS_STREAM_CATALOG.get(engine, {})
        self._model = cat.get("model", engine)
        self._default_voice = cat.get("voice", "")
        self._voice_ids = {v.get("voice_id") for v in cat.get("voices", [])}

    async def synthesize(self, text: str, voice_id: str, model: str,
                         speed: float, fmt: str):
        voice = voice_id if voice_id in self._voice_ids else ""
        prov = build_tts_stream_provider(self.engine, voice=voice)
        if prov is None:
            raise RuntimeError(f"TTS 引擎 {self.engine} 无可用 key")

        async def _once():
            yield text

        sr = int(TTS_STREAM_CATALOG.get(self.engine, {}).get("sample_rate") or 24000)
        chunks: list[bytes] = []
        async for item in prov.stream(_once(), voice=voice):
            if isinstance(item, dict):
                sr = int(item.get("sample_rate") or sr)
            elif item:
                chunks.append(item)
        pcm = b"".join(chunks)
        duration_ms = int(len(pcm) / (sr * 2) * 1000) if pcm else 0
        used_voice = voice or self._default_voice
        if fmt == "pcm16":
            return pcm, "pcm16", duration_ms, self._model, used_voice
        return _wav_header(len(pcm), sr) + pcm, "wav", duration_ms, self._model, used_voice

    async def list_voices(self, language: str, gender: str):
        voices = TTS_STREAM_CATALOG.get(self.engine, {}).get("voices", [])
        return [v for v in voices
                if (not language or v.get("language") == language)
                and (not gender or v.get("gender") == gender)]


def build_tts_provider(requested_provider: str = "") -> BaseTTSProvider:
    """批处理 TTS 工厂（/api/tts + gRPC Synthesize 共用，启动时装配）。
    TTS_PROVIDER：auto（默认）| mimo | cosyvoice | qwen | minimax | mock。
    ``requested_provider`` 非空时请求级 pin，优先级高于进程默认；用于需要冻结
    provider/voice 的验收夹具，也与流式端点的显式 provider 选择保持一致。
    auto：LLM_PROVIDER 为 MiMo 系且有 LLM_API_KEY → MiMo（历史现状）；否则桥接
    TTS_STREAM_PROVIDER 指定的流式引擎（需对应 key）；都不可用 → Mock。
    显式 mimo 复用 LLM_API_KEY（多 LLM 源惯例：该 env 即 MiMo 的 key）。"""
    choice = (
        requested_provider.strip().lower()
        or os.getenv("TTS_PROVIDER", "auto").strip().lower()
    )
    api_key = os.getenv("LLM_API_KEY", "")

    def _mock(why: str):
        _strict_mock_gate("tts", why)
        return MockTTSProvider()

    if choice == "mock":
        return _mock("TTS_PROVIDER=mock 显式指定")
    if choice in ("mimo", "xiaomimimo"):
        return MiMoTTSProvider(api_key) if api_key else _mock("mimo 引擎但无 LLM_API_KEY")
    if choice == "auto":
        llm_provider = os.getenv("LLM_PROVIDER", "xiaomimimo").lower()
        if llm_provider in ("xiaomimimo", "mimo") and api_key:
            return MiMoTTSProvider(api_key)
        choice = os.getenv("TTS_STREAM_PROVIDER", "cosyvoice").strip().lower()
        if choice == "dashscope":  # 泛指默认引擎（同 build_tts_stream_provider）
            choice = "cosyvoice"
    if choice in TTS_STREAM_CATALOG and build_tts_stream_provider(choice) is not None:
        return StreamBridgeTTSProvider(choice)
    return _mock("无可用引擎（流式引擎 key 均缺）")


# ─── 流式 TTS Provider（服务端 PCM 流式合成，R4.2）───
# 文本增量进、PCM 音频分片出。两个 DashScope（百炼）模型经 P0 探针实测（见
# docs/design/2026-07-04-r4.2-streaming-tts-bargein.md §7）：
#   - cosyvoice-v3-flash：run-task 协议（/api-ws/v1/inference，与 fun-asr 同壳），二进制音频帧，469ms 首帧
#   - qwen3-tts-flash-realtime：OpenAI-realtime 协议（/api-ws/v1/realtime，与 qwen3-asr 同壳），base64 音频，719ms 首帧
# 音色集互不相通（官方音色表；cosyvoice v2 名会 418）。HMI 两级选择：先选引擎（=provider）再选音色。

COSYVOICE_VOICES = [
    {"voice_id": "longxiaochun_v3", "name": "龙小淳", "language": "zh", "gender": "female",
     "description": "语音助手·女声", "tags": ["助手", "女声"]},
    {"voice_id": "longanwen_v3", "name": "龙安温", "language": "zh", "gender": "female",
     "description": "语音助手·女声", "tags": ["助手", "女声"]},
    {"voice_id": "longanyun_v3", "name": "龙安昀", "language": "zh", "gender": "male",
     "description": "语音助手·男声", "tags": ["助手", "男声"]},
    {"voice_id": "longhua_v3", "name": "龙华", "language": "zh", "gender": "female",
     "description": "社交陪伴·女声", "tags": ["陪伴", "女声"]},
    {"voice_id": "longze_v3", "name": "龙泽", "language": "zh", "gender": "male",
     "description": "社交陪伴·男声", "tags": ["陪伴", "男声"]},
    {"voice_id": "longanyang", "name": "龙安洋", "language": "zh", "gender": "male",
     "description": "社交陪伴·男声", "tags": ["陪伴", "男声"]},
    {"voice_id": "longanhuan_v3", "name": "龙安欢", "language": "zh", "gender": "female",
     "description": "多方言·女声", "tags": ["方言", "女声"]},
]

QWEN_TTS_VOICES = [
    {"voice_id": "Cherry", "name": "Cherry", "language": "zh", "gender": "female",
     "description": "中英双语·女声", "tags": ["双语", "女声"]},
    {"voice_id": "Serena", "name": "Serena", "language": "zh", "gender": "female",
     "description": "中英双语·女声", "tags": ["双语", "女声"]},
    {"voice_id": "Ethan", "name": "Ethan", "language": "zh", "gender": "male",
     "description": "中英双语·男声", "tags": ["双语", "男声"]},
    {"voice_id": "Chelsie", "name": "Chelsie", "language": "zh", "gender": "female",
     "description": "中英双语·女声", "tags": ["双语", "女声"]},
    {"voice_id": "Dylan", "name": "Dylan", "language": "zh", "gender": "male",
     "description": "北京话·男声", "tags": ["方言", "北京话"]},
    {"voice_id": "Jada", "name": "Jada", "language": "zh", "gender": "female",
     "description": "上海话·女声", "tags": ["方言", "上海话"]},
    {"voice_id": "Sunny", "name": "Sunny", "language": "zh", "gender": "female",
     "description": "四川话·女声", "tags": ["方言", "四川话"]},
]

# MiniMax T2A 系统音色（部分常用；完整表可调 MiniMax「查询可用音色」API）。
MINIMAX_VOICES = [
    {"voice_id": "female-tianmei", "name": "甜美女声", "language": "zh", "gender": "female",
     "description": "甜美·女声", "tags": ["女声"]},
    {"voice_id": "female-shaonv", "name": "少女音", "language": "zh", "gender": "female",
     "description": "少女·女声", "tags": ["女声"]},
    {"voice_id": "female-yujie", "name": "御姐音", "language": "zh", "gender": "female",
     "description": "御姐·女声", "tags": ["女声"]},
    {"voice_id": "male-qn-qingse", "name": "青涩青年", "language": "zh", "gender": "male",
     "description": "青涩·男声", "tags": ["男声"]},
    {"voice_id": "male-qn-jingying", "name": "精英青年", "language": "zh", "gender": "male",
     "description": "精英·男声", "tags": ["男声"]},
    {"voice_id": "presenter_female", "name": "女主持", "language": "zh", "gender": "female",
     "description": "主持·女声", "tags": ["女声", "主持"]},
    {"voice_id": "presenter_male", "name": "男主持", "language": "zh", "gender": "male",
     "description": "主持·男声", "tags": ["男声", "主持"]},
]

# provider id → (默认模型, 默认音色, 采样率, 音色表)。HMI/工厂共用，避免散落硬编码。
TTS_STREAM_CATALOG = {
    "cosyvoice": {"model": "cosyvoice-v3-flash", "voice": "longxiaochun_v3",
                  "sample_rate": 22050, "voices": COSYVOICE_VOICES, "label": "CosyVoice·流式"},
    "qwen": {"model": "qwen3-tts-flash-realtime", "voice": "Cherry",
             "sample_rate": 24000, "voices": QWEN_TTS_VOICES, "label": "Qwen·方言"},
    "mimo": {"model": "mimo-v2.5-tts", "voice": "冰糖",
             "sample_rate": 24000, "voices": MiMoTTSProvider.VOICES, "label": "MiMo·流式"},
    "minimax": {"model": os.getenv("MINIMAX_TTS_MODEL", "speech-2.8-turbo"),
                "voice": os.getenv("MINIMAX_TTS_VOICE", "female-tianmei"),
                "sample_rate": 24000, "voices": MINIMAX_VOICES, "label": "MiniMax·流式"},
}


# ── 句级切分：把「文本增量流」聚成「整句流」──
# MiMo/MiniMax 的 TTS API 都是「整段文本一次入」（不像 cosyvoice/qwen 支持 continue-task 增量喂），
# 故靠切句实现「边说边播」：每满一句就合成一段音频，逐段流式回。
_SENTENCE_END = "。！？!?…\n；;"


def _find_sentence_end(s: str) -> int:
    for i, ch in enumerate(s):
        if ch in _SENTENCE_END:
            return i
    return -1


# TTS 入口 markdown 清理：朗读文本可能来自任何历史/未来路径（speech 增量、卡片全文朗读、
# proactive 报告），星号/井号/表格符进合成会被读出来或产生怪停顿。与
# `agents/_sdk/grounding.strip_markdown_speech` 配对（llm-gateway 自包含服务，不跨包 import，
# 保持最小实现；口径变化两处同步）。
_TTS_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_TTS_MD_LINE = re.compile(r"(?m)^\s{0,3}(?:#{1,6}\s+|>\s?|[-*•]\s+|```.*$)")


def _strip_md_tts(text: str) -> str:
    t = text or ""
    if not any(ch in t for ch in ("*", "#", "`", "|", "[", ">")):
        return t
    t = _TTS_MD_LINK.sub(r"\1", t)
    t = _TTS_MD_LINE.sub("", t)
    t = t.replace("**", "").replace("`", "")
    t = t.replace("|", "，")            # 表格竖线读成顿号级停顿，不念符号
    return t.strip()


async def _sentence_segments(text_deltas, *, max_chars: int = 60):
    """异步生成器：缓冲文本增量，遇句末标点或超 max_chars 即吐一整句，收尾 flush 余量。
    句子组装完成后统一剥 markdown（跨增量的 ** 对已合并，此处剥不漏）。"""
    buf = ""
    async for delta in text_deltas:
        if not delta:
            continue
        buf += delta
        while True:
            idx = _find_sentence_end(buf)
            if idx == -1:
                break
            seg, buf = _strip_md_tts(buf[:idx + 1].strip()), buf[idx + 1:]
            if seg:
                yield seg
        if len(buf) >= max_chars:
            seg, buf = _strip_md_tts(buf.strip()), ""
            if seg:
                yield seg
    tail = _strip_md_tts(buf.strip())
    if tail:
        yield tail


class BaseStreamingTTSProvider:
    async def stream(
        self,
        text_deltas,
        *,
        voice: str = "",
        sample_rate: int = 0,
        instruct: str = "",
        speed: float = 0.0,
    ):
        """text_deltas：文本分片的异步迭代器（增量进）。
        yield bytes（PCM s16le 音频帧）或 dict（控制事件，如 {'type':'meta','sample_rate':..,'format':'pcm'}）。
        首个 yield 应为 meta（HMI 据此建 AudioContext 播放器）。"""
        raise NotImplementedError
        yield  # 标记为 async generator


# M2 P2（记忆图谱子 RFC §2.3）：会话级情绪标签 → TTS 指令化措辞。
# **措辞留在后端**：HMI 只传语义标签（happy/tired/...），不该知道某家 TTS 的 instruction
# 怎么写——换引擎时只改这里。词表外/neutral/空 → 空串 = 不发键（零行为变化）。
EMOTION_INSTRUCT = {
    "happy": "用轻快愉悦的语气说",
    "tired": "用放松柔和的语气慢慢说",
    "urgent": "用干脆利落的语气快速说",
    "frustrated": "用温和安抚的语气说",
}


def emotion_instruct(emotion: str) -> str:
    """情绪标签 → TTS instruction。未知/中性 → ""（缺省不发键）。"""
    return EMOTION_INSTRUCT.get((emotion or "").strip().lower(), "")


# ── 协议帧构造（纯函数，离线单测）──
def _cosyvoice_run_task(task_id: str, model: str, voice: str, sample_rate: int,
                        instruct: str = "", speed: float = 0.0) -> dict:
    """M1b C 件（母提案 §4.H 情感 TTS）：可选 instruct（指令化情感/风格，cosyvoice v3
    `instruction` 参数）与 speed（语速 `rate`，DashScope 取值 0.5~2.0）。缺省不发键=
    与既有请求字节级一致；话术层按情绪标签选参的接线等 §4.D emotion 落地（M2）。"""
    params: dict = {"text_type": "PlainText", "voice": voice,
                    "format": "pcm", "sample_rate": sample_rate}
    if instruct:
        params["instruction"] = instruct
    if speed and speed > 0:
        params["rate"] = max(0.5, min(2.0, float(speed)))
    return {"header": {"action": "run-task", "task_id": task_id, "streaming": "duplex"},
            "payload": {"task_group": "audio", "task": "tts", "function": "SpeechSynthesizer",
                        "model": model,
                        "parameters": params,
                        "input": {}}}


def _cosyvoice_continue(task_id: str, text: str) -> dict:
    return {"header": {"action": "continue-task", "task_id": task_id, "streaming": "duplex"},
            "payload": {"input": {"text": text}}}


def _cosyvoice_finish(task_id: str) -> dict:
    return {"header": {"action": "finish-task", "task_id": task_id, "streaming": "duplex"},
            "payload": {"input": {}}}


class DashScopeCosyVoiceProvider(BaseStreamingTTSProvider):
    """CosyVoice 流式 TTS——run-task 协议（/api-ws/v1/inference，与 fun-asr 同壳）。
    run-task→task-started→continue-task(每 delta)→二进制音频帧+result-generated→finish-task→task-finished。"""

    def __init__(self, api_key: str, ws_url: str, model: str,
                 voice: str = "longxiaochun_v3", sample_rate: int = 22050):
        self.api_key = api_key
        self.ws_url = ws_url
        self.model = model
        self.voice = voice
        self.sample_rate = sample_rate
        # M1b C 件：情感/语速默认经 env 注入（缺省空=零行为变化）；请求级参数化接线
        # 等 §4.D emotion 标签落地（M2）后走 stream() 形参。
        self.instruct = os.getenv("TTS_INSTRUCT_DEFAULT", "").strip()
        try:
            self.speed = float(os.getenv("TTS_SPEED_DEFAULT", "0") or 0)
        except ValueError:
            self.speed = 0.0

    async def stream(self, text_deltas, *, voice="", sample_rate=0,
                     instruct="", speed=0.0):
        import uuid

        import aiohttp
        voice = voice or self.voice
        sr = sample_rate or self.sample_rate
        task_id = uuid.uuid4().hex
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                self.ws_url, headers={"Authorization": f"bearer {self.api_key}"}, heartbeat=20.0,
            ) as ws:
                await ws.send_json(_cosyvoice_run_task(
                    task_id, self.model, voice, sr,
                    instruct=instruct or self.instruct, speed=speed or self.speed))
                started = asyncio.Event()

                async def pump():
                    try:
                        await started.wait()
                        async for delta in text_deltas:
                            if delta:
                                await ws.send_json(_cosyvoice_continue(task_id, delta))
                        await ws.send_json(_cosyvoice_finish(task_id))
                    except Exception:
                        pass  # 取消/断连时静默；WS 关闭即终止上游任务

                pump_task = asyncio.create_task(pump())
                try:
                    while True:
                        try:
                            msg = await ws.receive(timeout=30.0)
                        except asyncio.TimeoutError:
                            break
                        if msg.type == aiohttp.WSMsgType.BINARY:
                            yield bytes(msg.data)
                        elif msg.type == aiohttp.WSMsgType.TEXT:
                            evt = json.loads(msg.data).get("header", {}).get("event", "")
                            if evt == "task-started":
                                started.set()
                                yield {"type": "meta", "sample_rate": sr, "format": "pcm"}
                            elif evt == "task-finished":
                                break
                            elif evt == "task-failed":
                                raise RuntimeError(json.loads(msg.data).get("header", {})
                                                   .get("error_message", "cosyvoice task-failed"))
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR,
                                          aiohttp.WSMsgType.CLOSING):
                            break
                finally:
                    pump_task.cancel()


# ── qwen3-tts-flash-realtime 协议帧（OpenAI-realtime）──
def _qwen_session_update(voice: str, sample_rate: int) -> dict:
    return {"type": "session.update", "session": {
        "voice": voice, "response_format": "pcm", "sample_rate": sample_rate, "mode": "server_commit"}}


class DashScopeQwenTTSProvider(BaseStreamingTTSProvider):
    """Qwen3-TTS 流式 TTS——OpenAI-realtime 协议（/api-ws/v1/realtime，与 qwen3-asr 同壳）。
    session.created→session.update→input_text_buffer.append(每 delta,text)→response.audio.delta(base64)
    →commit/finish→response.audio.done→response.done。含北京/上海/四川方言音色。"""

    def __init__(self, api_key: str, ws_url: str, model: str,
                 voice: str = "Cherry", sample_rate: int = 24000):
        self.api_key = api_key
        self.ws_url = ws_url
        self.model = model
        self.voice = voice
        self.sample_rate = sample_rate

    async def stream(
        self,
        text_deltas,
        *,
        voice="",
        sample_rate=0,
        instruct="",
        speed=0.0,
    ):
        import base64

        import aiohttp
        del instruct, speed
        voice = voice or self.voice
        sr = sample_rate or self.sample_rate
        url = f"{self.ws_url}?model={self.model}"
        eid = [0]

        def ev(o):
            eid[0] += 1
            return {"event_id": f"ev{eid[0]}", **o}

        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                url, headers={"Authorization": f"Bearer {self.api_key}"}, heartbeat=20.0,
            ) as ws:
                for _ in range(5):  # 等 session.created
                    msg = await ws.receive(timeout=10)
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        if json.loads(msg.data).get("type") == "session.created":
                            break
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        raise RuntimeError("qwen-tts ws closed before session.created")
                await ws.send_json(ev(_qwen_session_update(voice, sr)))
                ready = asyncio.Event()

                async def pump():
                    try:
                        await ready.wait()
                        async for delta in text_deltas:
                            if delta:
                                await ws.send_json(ev({"type": "input_text_buffer.append", "text": delta}))
                        await ws.send_json(ev({"type": "input_text_buffer.commit"}))
                        await ws.send_json(ev({"type": "session.finish"}))
                    except Exception:
                        pass

                pump_task = asyncio.create_task(pump())
                meta_sent = False
                try:
                    while True:
                        try:
                            msg = await ws.receive(timeout=30.0)
                        except asyncio.TimeoutError:
                            break
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR,
                                            aiohttp.WSMsgType.CLOSING):
                                break
                            continue
                        m = json.loads(msg.data)
                        t = m.get("type", "")
                        if t == "session.updated":
                            ready.set()
                            if not meta_sent:
                                meta_sent = True
                                yield {"type": "meta", "sample_rate": sr, "format": "pcm"}
                        elif t == "response.audio.delta":
                            b64 = m.get("delta") or m.get("audio") or ""
                            if b64:
                                try:
                                    yield base64.b64decode(b64)
                                except Exception:
                                    pass
                        elif t in ("response.done", "session.finished"):
                            break
                        elif t == "error":
                            raise RuntimeError((m.get("error") or {}).get("message", "qwen-tts error"))
                finally:
                    pump_task.cancel()


class MockStreamingTTSProvider(BaseStreamingTTSProvider):
    """无 key/nightly 兜底：消费文本增量，按字数产出固定静音 PCM 分片（断言帧序而非音质）。"""

    def __init__(self, sample_rate: int = 24000):
        self.sample_rate = sample_rate

    async def stream(
        self,
        text_deltas,
        *,
        voice="",
        sample_rate=0,
        instruct="",
        speed=0.0,
    ):
        del voice, instruct, speed
        sr = sample_rate or self.sample_rate
        yield {"type": "meta", "sample_rate": sr, "format": "pcm"}
        async for delta in text_deltas:
            n = max(1, len(delta or ""))
            # 每字约 120ms 静音 @ sr（s16le），验证「文本增量→音频分片」链路
            yield b"\x00" * int(sr * 0.12) * 2 * n


class MiMoStreamingTTSProvider(BaseStreamingTTSProvider):
    """MiMo v2.5 流式 TTS（官方 stream:true + pcm16@24k）。TTS API 整段文本一次入 → 按句切分逐段
    流式合成、边说边播；每段一次 chat/completions(stream=true)，SSE 逐 chunk 取 delta.audio.data（base64 pcm16）。
    docs: https://mimo.mi.com/docs/zh-CN/quick-start/usage-guide/audio/speech-synthesis-v2.5"""

    def __init__(self, api_key: str, base_url: str = "", model: str = "mimo-v2.5-tts",
                 voice: str = "冰糖", sample_rate: int = 24000):
        self.api_key = api_key
        self.base_url = (base_url or os.getenv("MIMO_AUDIO_BASE_URL", "")
                         or MiMoTTSProvider.BASE_URL)
        self.model = model
        self.voice = voice
        self.sample_rate = sample_rate

    async def stream(
        self,
        text_deltas,
        *,
        voice="",
        sample_rate=0,
        instruct="",
        speed=0.0,
    ):
        import base64
        del instruct, speed
        voice = voice or self.voice
        sr = sample_rate or self.sample_rate
        headers = {"api-key": self.api_key, "Content-Type": "application/json",
                   "Accept-Encoding": "identity"}
        meta_sent = False
        async with httpx.AsyncClient(limits=_HTTP_LIMITS) as client:
            async for seg in _sentence_segments(text_deltas):
                body = {"model": self.model, "stream": True,
                        "messages": [{"role": "assistant", "content": seg}],
                        "audio": {"format": "pcm16", "voice": voice}}
                async with client.stream(
                        "POST", self.base_url, headers=headers, json=body,
                        timeout=httpx.Timeout(30, connect=_HTTP_CONNECT_S, pool=5.0)) as resp:
                    resp.raise_for_status()
                    if not meta_sent:
                        meta_sent = True
                        yield {"type": "meta", "sample_rate": sr, "format": "pcm"}
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if not payload or payload == "[DONE]":
                            continue
                        try:
                            chunk = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        delta = ((chunk.get("choices") or [{}])[0].get("delta") or {})
                        audio = (delta.get("audio") or {})
                        b64 = audio.get("data") or ""
                        if b64:
                            try:
                                yield base64.b64decode(b64)
                            except (ValueError, TypeError):
                                pass
        if not meta_sent:
            yield {"type": "meta", "sample_rate": sr, "format": "pcm"}


class MiniMaxStreamingTTSProvider(BaseStreamingTTSProvider):
    """MiniMax T2A v2 流式 TTS（stream:true → SSE，音频 hex 编码在 data.audio）。整段文本一次入 →
    按句切分逐段流式合成。与 MiniMax LLM 同一把 MINIMAX_API_KEY（Bearer）。
    docs: https://platform.minimaxi.com/docs/api-reference/speech-t2a-http.md"""

    def __init__(self, api_key: str, url: str = "", model: str = "speech-2.8-turbo",
                 voice: str = "female-tianmei", sample_rate: int = 24000):
        self.api_key = api_key
        self.url = url or os.getenv("MINIMAX_T2A_URL", "https://api.minimaxi.com/v1/t2a_v2")
        self.model = model
        self.voice = voice
        self.sample_rate = sample_rate

    async def stream(
        self,
        text_deltas,
        *,
        voice="",
        sample_rate=0,
        instruct="",
        speed=0.0,
    ):
        del instruct, speed
        voice = voice or self.voice
        sr = sample_rate or self.sample_rate
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        meta_sent = False
        async with httpx.AsyncClient(limits=_HTTP_LIMITS) as client:
            async for seg in _sentence_segments(text_deltas):
                body = {"model": self.model, "text": seg, "stream": True,
                        "voice_setting": {"voice_id": voice, "speed": 1.0},
                        "audio_setting": {"sample_rate": sr, "format": "pcm", "channel": 1}}
                async with client.stream(
                        "POST", self.url, headers=headers, json=body,
                        timeout=httpx.Timeout(30, connect=_HTTP_CONNECT_S, pool=5.0)) as resp:
                    resp.raise_for_status()
                    if not meta_sent:
                        meta_sent = True
                        yield {"type": "meta", "sample_rate": sr, "format": "pcm"}
                    got_incremental = False
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if not payload or payload == "[DONE]":
                            continue
                        try:
                            chunk = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        data = chunk.get("data") or {}
                        audio_hex = data.get("audio") or ""
                        # MiniMax T2A 流式：status=1 是增量音频块；status=2 是「整段音频汇总重发」（= 全部
                        # 增量拼起来，重复！）。已收到增量则跳过 status=2 防双份播放；仅当只有汇总（极短
                        # 文本无增量）时才用它。收到 status=2 即本段结束。
                        if data.get("status") == 2:
                            if not got_incremental and audio_hex:
                                try:
                                    yield bytes.fromhex(audio_hex)
                                except ValueError:
                                    pass
                            break
                        if audio_hex:
                            got_incremental = True
                            try:
                                yield bytes.fromhex(audio_hex)
                            except ValueError:
                                pass
        if not meta_sent:
            yield {"type": "meta", "sample_rate": sr, "format": "pcm"}


def build_tts_stream_provider(provider: str = "", model: str = "",
                              voice: str = "") -> "BaseStreamingTTSProvider | None":
    """按请求/env 选流式 TTS 引擎。provider/model/voice 为请求级覆盖（HMI 设置可切），空则 env 默认。
    无可用 key 或 off → None（HMI 探测到则无感回退句级批处理 /api/tts，惯例同 ASR 流式）。"""
    provider = (provider or os.getenv("TTS_STREAM_PROVIDER", "cosyvoice")).strip().lower()
    if provider in ("", "off", "none"):
        return None
    if provider == "mock":
        return MockStreamingTTSProvider()
    if provider == "mimo":
        key = os.getenv("LLM_API_KEY", "")
        if not key:
            return None
        cat = TTS_STREAM_CATALOG["mimo"]
        return MiMoStreamingTTSProvider(key, model=model or cat["model"],
                                        voice=voice or cat["voice"], sample_rate=cat["sample_rate"])
    if provider == "minimax":
        key = os.getenv("MINIMAX_API_KEY", "")
        if not key:
            return None
        cat = TTS_STREAM_CATALOG["minimax"]
        return MiniMaxStreamingTTSProvider(key, model=model or cat["model"],
                                           voice=voice or cat["voice"], sample_rate=cat["sample_rate"])
    if provider in ("cosyvoice", "qwen", "dashscope"):
        key = os.getenv("DASHSCOPE_ASR_KEY") or os.getenv("LLM_EMBED_API_KEY", "")
        if not key:
            return None
        if provider == "dashscope":  # 泛指默认引擎
            provider = "cosyvoice"
        cat = TTS_STREAM_CATALOG[provider]
        mdl = model or os.getenv("TTS_STREAM_MODEL", "") or cat["model"]
        if provider == "qwen":
            ws_url = os.getenv("DASHSCOPE_TTS_REALTIME_WS_URL",
                               "wss://dashscope.aliyuncs.com/api-ws/v1/realtime")
            return DashScopeQwenTTSProvider(key, ws_url, mdl, voice=voice or cat["voice"],
                                            sample_rate=cat["sample_rate"])
        ws_url = os.getenv("DASHSCOPE_TTS_INFERENCE_WS_URL",
                           "wss://dashscope.aliyuncs.com/api-ws/v1/inference")
        return DashScopeCosyVoiceProvider(key, ws_url, mdl, voice=voice or cat["voice"],
                                          sample_rate=cat["sample_rate"])
    return None
