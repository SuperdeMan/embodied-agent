"""HRI console server contract tests over the hri.v0 WS protocol — a scripted client
plays the browser role; sim/engine/audio are fakes (hermetic, no mujoco, no network)."""

from __future__ import annotations

import asyncio
import json

import numpy as np
import pytest
from aiohttp.test_utils import TestClient, TestServer

from embodied.cognition.plan import Plan, Step, StepResult, StepStatus
from embodied.hri.server import AppContext, build_app
from embodied.skills.manifest import SkillManifest
from embodied.skills.registry import SkillRegistry, SkillResult


class FakeSpec:
    embodiment_id = "sim.fake"


class FakeSim:
    def spec(self):
        return FakeSpec()

    def render(self, camera="side"):
        return np.zeros((32, 48, 3), dtype=np.uint8)


class FakeASR:
    """Mirrors the real BaseASRProvider contract: 5-tuple return, WAV input."""

    def __init__(self, text="把红色方块放进盒子"):
        self.text = text
        self.audio_seen = b""

    async def transcribe(self, audio, fmt, language, model):
        self.audio_seen = audio
        return self.text, 0.9, language or "zh", "fake", 3


class FakeTTS:
    """Mirrors the real BaseStreamingTTSProvider contract: meta dict first, then PCM."""

    async def stream(self, text_deltas, *, sample_rate=24000, **kw):
        async for _ in text_deltas:
            pass
        yield {"type": "meta", "sample_rate": 22050, "format": "pcm"}
        yield b"\x01\x00" * 160
        yield b"\x02\x00" * 160


class FakeTurn:
    def __init__(self, text, results=(), plan=None):
        self.text = text
        self.results = list(results)
        self.plan = plan
        self.replans = 0


def make_ctx(engine_factory=None, asr=None):
    registry = SkillRegistry()

    async def noop(**kw):
        return SkillResult(ok=True)

    registry.register(SkillManifest(name="skill.test.noop", description="noop"), noop)

    class FakeEngine:
        def __init__(self, confirm):
            self.confirm = confirm

        async def turn(self, text):
            plan = Plan(steps=[Step(id="s1", skill="skill.test.noop")])
            results = [StepResult(step_id="s1", status=StepStatus.OK, detail="did it")]
            return FakeTurn(f"echo: {text}", results, plan)

    factory = engine_factory or (lambda confirm: FakeEngine(confirm))
    return AppContext(FakeSim(), registry, factory, asr or FakeASR(), FakeTTS())


@pytest.fixture
async def client(request):
    ctx = getattr(request, "param", None) or make_ctx()
    server = TestServer(build_app(ctx))
    client = TestClient(server)
    await client.start_server()
    yield client
    await client.close()


async def recv_json(ws, want_type=None, *, max_frames=20):
    """Read frames until the next JSON text frame (optionally of a given type)."""
    for _ in range(max_frames):
        msg = await asyncio.wait_for(ws.receive(), timeout=5)
        if msg.type.name == "TEXT":
            data = json.loads(msg.data)
            if want_type is None or data.get("type") == want_type:
                return data
        elif msg.type.name == "BINARY":
            continue
    raise AssertionError(f"no {want_type} frame within {max_frames} frames")


async def test_hello_and_text_turn(client):
    async with client.ws_connect("/ws/session") as ws:
        hello = await recv_json(ws, "hello")
        assert hello["embodiment"] == "sim.fake"
        assert hello["skills"][0]["name"] == "skill.test.noop"

        await ws.send_str(json.dumps({"type": "text", "text": "你好机器人"}))
        assert (await recv_json(ws, "turn_start"))["type"] == "turn_start"
        step = await recv_json(ws, "step")
        assert step["step_id"] == "s1" and step["status"] == "ok" and step["skill"] == "skill.test.noop"
        reply = await recv_json(ws, "reply")
        assert reply["text"] == "echo: 你好机器人"
        tts = await recv_json(ws, "tts_start")
        assert tts["sample_rate"] == 22050  # provider meta wins over configured default
        pcm = bytearray()
        while True:
            msg = await asyncio.wait_for(ws.receive(), timeout=5)
            if msg.type.name == "BINARY":
                pcm.extend(msg.data)
            elif msg.type.name == "TEXT" and json.loads(msg.data).get("type") == "tts_end":
                break
        assert len(pcm) == 2 * 320  # both fake chunks arrived


async def test_voice_turn_streams_pcm_to_asr():
    asr = FakeASR("回零")
    ctx = make_ctx(asr=asr)
    server = TestServer(build_app(ctx))
    client = TestClient(server)
    await client.start_server()
    try:
        async with client.ws_connect("/ws/session") as ws:
            await recv_json(ws, "hello")
            await ws.send_str(json.dumps({"type": "audio_start"}))
            await ws.send_bytes(b"\x00\x01" * 800)
            await ws.send_bytes(b"\x02\x03" * 800)
            await ws.send_str(json.dumps({"type": "audio_end"}))
            asr_frame = await recv_json(ws, "asr")
            assert asr_frame["text"] == "回零" and asr_frame["final"] is True
            reply = await recv_json(ws, "reply")
            assert reply["text"] == "echo: 回零"
        # both chunks reached the provider intact, wrapped in a 44-byte WAV header
        assert len(asr.audio_seen) == 44 + 2 * 1600
        assert asr.audio_seen[:4] == b"RIFF"
    finally:
        await client.close()


async def test_confirm_bridge_roundtrip():
    """Danger gate reaches the browser and the reply resolves the engine's future."""
    outcomes = []

    class ConfirmingEngine:
        def __init__(self, confirm):
            self.confirm = confirm

        async def turn(self, text):
            approved = await self.confirm("skill.system.power_off", {"reason": "test"})
            outcomes.append(approved)
            status = StepStatus.OK if approved else StepStatus.NEED_CONFIRM
            return FakeTurn("done" if approved else "denied",
                            [StepResult(step_id="s1", status=status)])

    ctx = make_ctx(engine_factory=lambda confirm: ConfirmingEngine(confirm))
    server = TestServer(build_app(ctx))
    client = TestClient(server)
    await client.start_server()
    try:
        async with client.ws_connect("/ws/session") as ws:
            await recv_json(ws, "hello")
            await ws.send_str(json.dumps({"type": "text", "text": "关机"}))
            req = await recv_json(ws, "confirm_request")
            assert req["skill"] == "skill.system.power_off"
            await ws.send_str(json.dumps({"type": "confirm_reply", "rid": req["rid"], "approved": True}))
            reply = await recv_json(ws, "reply")
            assert reply["text"] == "done"
        assert outcomes == [True]
    finally:
        await client.close()


async def test_scene_endpoint_serves_jpeg(client):
    resp = await client.get("/api/scene.jpg")
    assert resp.status == 200
    assert resp.content_type == "image/jpeg"
    body = await resp.read()
    assert body[:2] == b"\xff\xd8"  # JPEG SOI


async def test_bad_json_yields_error_frame(client):
    async with client.ws_connect("/ws/session") as ws:
        await recv_json(ws, "hello")
        await ws.send_str("{not json")
        err = await recv_json(ws, "error")
        assert "bad json" in err["message"]
