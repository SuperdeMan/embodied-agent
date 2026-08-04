# Ported from car-agent observability/tests/test_events.py + test_turn_events.py @ f0b08f8, changes: NATS transport tests replaced by fresh sink tests (JsonlSink/StdoutSink/OBS_DIR/failure isolation); payload-shape and session-injection assertions kept.
"""EventEmitter + sinks：JSONL 落盘、OBS_DIR/OBS_STDOUT 门控、失败隔离、payload 形状。"""
import json

import pytest

from embodied.runtime.obs.events import EventEmitter, JsonlSink, StdoutSink, get_emitter
from embodied.runtime.obs.tracing import set_session_id


@pytest.fixture(autouse=True)
def _clear_session():
    set_session_id("")
    yield
    set_session_id("")


class _ListSink:
    def __init__(self):
        self.sent: list[tuple[str, dict]] = []

    def emit(self, subject, payload):
        self.sent.append((subject, payload))


class _BoomSink:
    def __init__(self):
        self.calls = 0

    def emit(self, subject, payload):
        self.calls += 1
        raise RuntimeError("sink down")


# ── 传输层（fresh：sink 重设计）──────────────────────────────────────────

async def test_jsonl_sink_writes_valid_jsonl_and_honors_obs_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("OBS_DIR", str(tmp_path / "obs"))
    monkeypatch.delenv("OBS_STDOUT", raising=False)
    emitter = EventEmitter("runtime-test")
    await emitter.emit_span("t1", "planner.step", duration_ms=12.34)
    await emitter.emit_metric("skill.manip.pick", 3, 120.0, 0.0)
    await emitter.close()

    path = tmp_path / "obs" / "events.jsonl"
    assert path.exists()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    bodies = [json.loads(line) for line in lines]      # 每行都是合法 JSON
    assert bodies[0]["subject"] == "obs.span"
    assert bodies[0]["service"] == "runtime-test"
    assert bodies[1]["subject"] == "obs.metric"


async def test_obs_dir_defaults_to_outputs_obs_under_cwd(tmp_path, monkeypatch):
    monkeypatch.delenv("OBS_DIR", raising=False)
    monkeypatch.delenv("OBS_STDOUT", raising=False)
    monkeypatch.chdir(tmp_path)
    emitter = EventEmitter("runtime-test")
    await emitter.emit_span("t1", "planner.step")
    await emitter.close()
    assert (tmp_path / "outputs" / "obs" / "events.jsonl").exists()


def test_stdout_sink_gated_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("OBS_DIR", str(tmp_path))
    monkeypatch.delenv("OBS_STDOUT", raising=False)
    assert [type(s) for s in EventEmitter("x").sinks] == [JsonlSink]
    monkeypatch.setenv("OBS_STDOUT", "1")
    assert [type(s) for s in EventEmitter("x").sinks] == [JsonlSink, StdoutSink]


async def test_stdout_sink_writes_json_line(monkeypatch, capsys):
    emitter = EventEmitter("runtime-test", sinks=[StdoutSink()])
    await emitter.emit_span("t-out", "planner.step")
    await emitter.close()
    out = capsys.readouterr().out.strip()
    body = json.loads(out)
    assert body["subject"] == "obs.span"
    assert body["trace_id"] == "t-out"


async def test_emit_never_raises_when_sink_fails():
    """Sink 抛错必须被吞掉（log-and-drop）：观测绝不反噬主链路。"""
    boom = _BoomSink()
    emitter = EventEmitter("runtime-test", sinks=[boom])
    await emitter.emit_span("t1", "planner.step")
    await emitter.emit_turn("t1", "s1", user_text="拿一下桌上的杯子")
    await emitter.flush()          # 不抛
    assert boom.calls == 2
    await emitter.close()


async def test_failing_sink_does_not_block_other_sinks():
    boom, ok = _BoomSink(), _ListSink()
    emitter = EventEmitter("runtime-test", sinks=[boom, ok])
    await emitter.emit_span("t1", "planner.step")
    await emitter.close()
    assert boom.calls == 1
    assert [s for s, _ in ok.sent] == ["obs.span"]


async def test_emit_is_noop_with_no_sinks():
    """空 sink 列表 = 禁用：不抛、不排队。"""
    emitter = EventEmitter("runtime-test", sinks=[])
    await emitter.emit_span("t1", "planner.step")
    assert emitter._disabled is True
    assert emitter._queue.empty()


def test_get_emitter_returns_one_instance_per_service(tmp_path, monkeypatch):
    monkeypatch.setenv("OBS_DIR", str(tmp_path))
    a = get_emitter("svc-a")
    assert get_emitter("svc-a") is a
    assert get_emitter("svc-b") is not a


# ── payload 形状（ported）────────────────────────────────────────────────

def _capture_emitter():
    sink = _ListSink()
    return EventEmitter("runtime-test", sinks=[sink]), sink


async def test_emit_span_payload_shape():
    emitter, sink = _capture_emitter()
    await emitter.emit_span(
        "trace-9", "step.skill:manip.pick", status="ok", duration_ms=340,
        attrs={"intent": "manip.pick"})
    await emitter.close()
    subject, body = sink.sent[0]
    assert subject == "obs.span"
    assert body["trace_id"] == "trace-9"
    assert body["node"] == "step.skill:manip.pick"
    assert body["service"] == "runtime-test"
    assert body["attrs"]["intent"] == "manip.pick"
    assert "ts" in body and "span_id" in body


async def test_emit_turn_payload_shape():
    emitter, sink = _capture_emitter()
    await emitter.emit_turn(
        "trace-1", "sess-1",
        user_text="把桌上的杯子递给我", speech="好的，马上拿",
        status="ok", path="local", input_source="voice_wake",
        is_confirmation=False, ui_card_type="", actions=1,
        duration_ms=12.34, ts=1720000000000)
    await emitter.close()
    subject, body = sink.sent[0]
    assert subject == "obs.turn"
    assert body["trace_id"] == "trace-1"
    assert body["session_id"] == "sess-1"
    assert body["user_text"] == "把桌上的杯子递给我"
    assert body["speech"] == "好的，马上拿"
    assert body["status"] == "ok"
    assert body["actions"] == 1
    assert body["ts"] == 1720000000000
    assert body["duration_ms"] == 12.3


async def test_emit_turn_respects_content_gate(monkeypatch):
    monkeypatch.setenv("OBS_CONTENT_CAPTURE", "off")
    emitter, sink = _capture_emitter()
    await emitter.emit_turn("t", "s", user_text="去厨房拿水", speech="好的")
    await emitter.close()
    body = sink.sent[0][1]
    assert "厨房" not in body["user_text"]
    assert body["user_text"].startswith("<len=")


async def test_span_auto_carries_session_from_contextvar():
    emitter, sink = _capture_emitter()
    set_session_id("sess-ctx")
    await emitter.emit_span("trace-2", "route.local")
    await emitter.close()
    assert sink.sent[0][1]["session_id"] == "sess-ctx"


async def test_span_without_session_context_stays_clean():
    emitter, sink = _capture_emitter()
    await emitter.emit_span("trace-3", "route.local")
    await emitter.close()
    assert "session_id" not in sink.sent[0][1]


async def test_emit_llm_payload():
    emitter, sink = _capture_emitter()
    await emitter.emit_llm(
        trace_id="t-llm", session_id="s-llm", caller="planner",
        model="mimo-v2.5", prompt_tokens=100, completion_tokens=50,
        latency_ms=321.7, cache_hit=False, thinking=True,
        prompt_tail="用户说: 你好", content_head='{"steps":[]}')
    await emitter.close()
    subject, body = sink.sent[0]
    assert subject == "obs.llm"
    assert body["caller"] == "planner"
    assert body["model"] == "mimo-v2.5"
    assert body["thinking"] is True
    assert body["prompt_tail"] == "用户说: 你好"


async def test_emit_state_uses_robot_subject():
    emitter, sink = _capture_emitter()
    await emitter.emit_state(
        [{"key": "arm_joint_1", "old": 0, "new": 30}], "reflex", trace_id="t-s")
    await emitter.close()
    subject, body = sink.sent[0]
    assert subject == "robot.state.changed"
    assert body["source"] == "reflex"
    assert body["changes"][0]["key"] == "arm_joint_1"
