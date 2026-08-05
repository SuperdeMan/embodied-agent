"""HRI console server: browser voice/text control over one WebSocket + scene frames.

Endpoint contract (versioned informally as hri.v0, console/app.js is the only client):

WS /ws/session — JSON text frames down, JSON or binary frames up:
  up:   {"type":"text","text":...}                    text command
        {"type":"audio_start"}                        begin an utterance (push-to-talk press)
        <binary>                                      16 kHz mono s16le PCM chunk
        {"type":"audio_end"}                          utterance finished (button release)
  down: {"type":"hello","skills":[...],"embodiment":...}
        {"type":"asr","text":...,"final":true|false}
        {"type":"turn_start"}
        {"type":"step","step_id":...,"skill":...,"status":...,"detail":...}
        {"type":"reply","text":...}
        {"type":"tts_start","sample_rate":N} / <binary PCM> / {"type":"tts_end"}
        {"type":"confirm_request","rid":...,"skill":...,"params":{...}}   (danger gate)
        {"type":"error","message":...}
  up:   {"type":"confirm_reply","rid":...,"approved":true|false}

GET /api/scene.jpg — current sim render (side camera), for <img> polling.
GET /           — static console from console/.

Voice v0 is push-to-talk: endpoint authority stays client-side (the car-agent real-device
lesson: provider-side silence detection deadlocks against client finalization; carried
here by design even though v0 has no VAD yet).
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import uuid
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

logger = logging.getLogger("hri.server")

CONSOLE_DIR = Path(__file__).resolve().parents[3] / "console"
PCM_SAMPLE_RATE = 16000
CTX_KEY = web.AppKey("ctx", object)


class ConsoleSession:
    """One websocket = one conversation session with its own engine history."""

    def __init__(self, app_ctx: "AppContext", ws: web.WebSocketResponse) -> None:
        self.ctx = app_ctx
        self.ws = ws
        self.audio_buf = bytearray()
        self.recording = False
        self._pending_confirms: dict[str, asyncio.Future[bool]] = {}
        self.engine = app_ctx.make_engine(self._confirm)
        self._turn_lock = asyncio.Lock()  # one turn at a time per session
        # Turns run as background tasks: the WS read loop must stay free to receive
        # confirm_reply frames while a turn is blocked inside the confirm bridge —
        # awaiting the turn inline deadlocks the danger gate (caught by contract test).
        self._tasks: set[asyncio.Task] = set()

    # -- confirm bridge (danger gate reaches the browser) ----------------------

    async def _confirm(self, skill: str, params: dict[str, Any]) -> bool:
        rid = uuid.uuid4().hex[:8]
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._pending_confirms[rid] = fut
        await self.send({"type": "confirm_request", "rid": rid, "skill": skill, "params": params})
        try:
            return await asyncio.wait_for(fut, timeout=30.0)
        except asyncio.TimeoutError:
            return False  # fail-closed: no answer = denied
        finally:
            self._pending_confirms.pop(rid, None)

    def resolve_confirm(self, rid: str, approved: bool) -> None:
        fut = self._pending_confirms.get(rid)
        if fut is not None and not fut.done():
            fut.set_result(bool(approved))

    # -- io --------------------------------------------------------------------

    async def send(self, payload: dict[str, Any]) -> None:
        if not self.ws.closed:
            await self.ws.send_str(json.dumps(payload, ensure_ascii=False))

    async def send_pcm(self, chunk: bytes) -> None:
        if not self.ws.closed and chunk:
            await self.ws.send_bytes(chunk)

    # -- message handling ------------------------------------------------------

    async def handle_text_frame(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            await self.send({"type": "error", "message": "bad json"})
            return
        kind = msg.get("type")
        if kind == "text":
            text = str(msg.get("text", "")).strip()
            if text:
                self._spawn(self.run_turn(text))
        elif kind == "audio_start":
            self.audio_buf.clear()
            self.recording = True
        elif kind == "audio_end":
            self.recording = False
            pcm = bytes(self.audio_buf)
            self.audio_buf.clear()
            if pcm:
                self._spawn(self.run_voice_turn(pcm))
        elif kind == "confirm_reply":
            self.resolve_confirm(str(msg.get("rid", "")), bool(msg.get("approved")))
        else:
            await self.send({"type": "error", "message": f"unknown type {kind!r}"})

    def handle_binary_frame(self, data: bytes) -> None:
        if self.recording:
            self.audio_buf.extend(data)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for fut in self._pending_confirms.values():
            if not fut.done():
                fut.set_result(False)  # fail-closed on disconnect

    # -- turns -----------------------------------------------------------------

    async def run_voice_turn(self, pcm: bytes) -> None:
        try:
            text = await self.ctx.transcribe(pcm)
        except Exception as e:
            logger.warning("asr failed: %s", e)
            await self.send({"type": "error", "message": f"asr failed: {e}"})
            return
        await self.send({"type": "asr", "text": text, "final": True})
        if text.strip():
            await self.run_turn(text)

    async def run_turn(self, text: str) -> None:
        async with self._turn_lock:
            await self.send({"type": "turn_start"})
            try:
                turn = await self.engine.turn(text)
            except Exception as e:
                logger.exception("engine turn failed")
                await self.send({"type": "error", "message": f"turn failed: {e}"})
                return
            for r in turn.results:
                plan_step = next(
                    (s for s in (turn.plan.steps if turn.plan else []) if s.id == r.step_id), None
                )
                await self.send({
                    "type": "step", "step_id": r.step_id,
                    "skill": plan_step.skill if plan_step else "",
                    "status": r.status.value, "detail": r.detail or r.error,
                })
            await self.send({"type": "reply", "text": turn.text})
            await self._speak(turn.text)

    async def _speak(self, text: str) -> None:
        """TTS stream contract (providers/audio.py): first a {"type":"meta","sample_rate":N}
        dict, then raw PCM byte chunks. tts_start carries the provider-declared rate."""
        started = False
        try:
            async for item in self.ctx.synthesize_stream(text):
                if isinstance(item, dict):
                    if not started:
                        started = True
                        await self.send({
                            "type": "tts_start",
                            "sample_rate": int(item.get("sample_rate") or self.ctx.tts_sample_rate),
                        })
                    continue
                if not started:  # provider skipped meta: fall back to configured rate
                    started = True
                    await self.send({"type": "tts_start", "sample_rate": self.ctx.tts_sample_rate})
                await self.send_pcm(item)
            if not started:
                await self.send({"type": "tts_start", "sample_rate": self.ctx.tts_sample_rate})
            await self.send({"type": "tts_end"})
        except Exception as e:
            logger.warning("tts failed: %s", e)
            await self.send({"type": "error", "message": f"tts failed: {e}"})


class AppContext:
    """Wires sim + registry + engine factory + audio providers; owned by `embodied console`."""

    def __init__(
        self, sim: Any, registry: Any, make_engine, asr: Any, tts_stream: Any,
        tts_sample_rate: int = 24000,
    ):
        self.sim = sim
        self.registry = registry
        self.make_engine = make_engine  # (confirm_cb) -> PlannerEngine
        self._asr = asr
        self._tts_stream = tts_stream
        self.tts_sample_rate = tts_sample_rate
        self._render_lock = asyncio.Lock()

    async def transcribe(self, pcm: bytes) -> str:
        from embodied.providers.audio import _wav_header

        # Console uplink is raw 16 kHz mono PCM16; batch ASR providers expect WAV.
        wav = _wav_header(len(pcm), PCM_SAMPLE_RATE) + pcm
        out = await self._asr.transcribe(wav, "wav", "zh", "")
        # Provider contract: (text, confidence, language, provider, latency_ms)
        return str(out[0]) if isinstance(out, tuple) else str(out)

    async def synthesize_stream(self, text: str):
        async def deltas():
            yield text

        async for chunk in self._tts_stream.stream(deltas(), sample_rate=self.tts_sample_rate):
            yield chunk

    async def render_jpeg(self) -> bytes:
        from PIL import Image

        async with self._render_lock:
            frame = await asyncio.to_thread(self.sim.render, "side")
        buf = io.BytesIO()
        Image.fromarray(frame).save(buf, format="JPEG", quality=80)
        return buf.getvalue()


# -- aiohttp wiring -----------------------------------------------------------


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    ctx: AppContext = request.app[CTX_KEY]  # type: ignore[assignment]
    session = ConsoleSession(ctx, ws)
    skills = [
        {"name": m.name, "description": m.description, "require_confirm": m.require_confirm}
        for m in ctx.registry.catalog()
    ]
    await session.send({
        "type": "hello", "skills": skills,
        "embodiment": ctx.sim.spec().embodiment_id, "pcm_sample_rate": PCM_SAMPLE_RATE,
    })
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                await session.handle_text_frame(msg.data)
            elif msg.type == WSMsgType.BINARY:
                session.handle_binary_frame(msg.data)
            elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break
    finally:
        await session.close()
    return ws


async def scene_handler(request: web.Request) -> web.Response:
    ctx: AppContext = request.app[CTX_KEY]  # type: ignore[assignment]
    try:
        data = await ctx.render_jpeg()
    except Exception as e:
        return web.Response(status=503, text=f"render unavailable: {e}")
    return web.Response(body=data, content_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})


async def index_handler(request: web.Request) -> web.FileResponse:
    return web.FileResponse(CONSOLE_DIR / "index.html")


def build_app(ctx: AppContext) -> web.Application:
    app = web.Application()
    app[CTX_KEY] = ctx
    app.router.add_get("/ws/session", ws_handler)
    app.router.add_get("/api/scene.jpg", scene_handler)
    app.router.add_get("/", index_handler)
    app.router.add_static("/console", CONSOLE_DIR)
    return app


def run_console(ctx: AppContext, host: str = "127.0.0.1", port: int = 8390) -> None:
    app = build_app(ctx)
    logger.info("console at http://%s:%d", host, port)
    print(f"console ready: http://{host}:{port}  (Ctrl+C to stop)")
    web.run_app(app, host=host, port=port, print=None)
