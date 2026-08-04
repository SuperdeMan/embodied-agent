# Ported from car-agent observability/tests/test_redact.py @ f0b08f8, changes: import path only.
"""redact.py：共享脱敏 + 内容采集门控。"""
from embodied.runtime.obs.redact import content_capture_enabled, gate_content, redact


def test_redact_masks_sensitive_fields():
    assert "***" in redact("token=abc123secret")
    assert "***" in redact("password: hunter2")
    assert "***" in redact("api_key=sk-xxxx")
    assert "***" in redact("打给 13800138000 吧")


def test_gate_content_on_redacts_and_truncates(monkeypatch):
    monkeypatch.delenv("OBS_CONTENT_CAPTURE", raising=False)
    assert content_capture_enabled() is True
    out = gate_content("token=secret 后面是正文", 100)
    assert "secret" not in out
    long = gate_content("很长" * 200, 10)
    assert len(long) == 11 and long.endswith("…")


def test_gate_content_off_keeps_only_fingerprint(monkeypatch):
    monkeypatch.setenv("OBS_CONTENT_CAPTURE", "off")
    out = gate_content("帮我拿一下遥控器")
    assert "遥控器" not in out
    assert out.startswith("<len=8 sha=")


def test_gate_content_empty_passthrough():
    assert gate_content("") == ""
