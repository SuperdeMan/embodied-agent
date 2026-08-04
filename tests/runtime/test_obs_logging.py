# Ported from car-agent observability/tests/test_logging_ship.py @ f0b08f8, changes: NatsLogHandler tests dropped with the handler; fresh StructuredFormatter + setup_logging (console / LOG_FILE) coverage.
"""logging：结构化 JSON 格式、脱敏、trace/session 注入、setup_logging handler 装配。"""
import json
import logging

import pytest

from embodied.runtime.obs.logging import StructuredFormatter, setup_logging
from embodied.runtime.obs.tracing import set_session_id, set_trace_id


@pytest.fixture(autouse=True)
def _clean_ctx():
    set_trace_id("")
    set_session_id("")
    yield
    set_trace_id("")
    set_session_id("")


@pytest.fixture()
def _restore_root_logger():
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    yield root
    for h in root.handlers:
        if h not in saved_handlers:
            h.close()
    root.handlers = saved_handlers
    root.setLevel(saved_level)


def _record(msg: str, level=logging.INFO, extra_data: dict | None = None) -> logging.LogRecord:
    record = logging.LogRecord("test.logger", level, __file__, 1, msg, None, None)
    if extra_data is not None:
        record.extra_data = extra_data
    return record


def test_formatter_outputs_json_with_core_fields():
    out = StructuredFormatter(datefmt="%Y-%m-%dT%H:%M:%S").format(_record("hello"))
    body = json.loads(out)
    assert body["level"] == "INFO"
    assert body["logger"] == "test.logger"
    assert body["msg"] == "hello"
    assert "ts" in body


def test_formatter_injects_trace_and_session_ids():
    set_trace_id("trace-log-1")
    set_session_id("sess-log-1")
    body = json.loads(StructuredFormatter().format(_record("with ids")))
    assert body["trace_id"] == "trace-log-1"
    assert body["session_id"] == "sess-log-1"


def test_formatter_omits_ids_when_unset():
    body = json.loads(StructuredFormatter().format(_record("clean")))
    assert "trace_id" not in body and "session_id" not in body


def test_formatter_merges_extra_data():
    body = json.loads(StructuredFormatter().format(
        _record("x", extra_data={"skill": "manip.pick"})))
    assert body["skill"] == "manip.pick"


def test_formatter_redacts_sensitive_fields():
    out = StructuredFormatter().format(_record("token=secret-value leaked"))
    assert "secret-value" not in out
    assert "token=***" in out


def test_setup_logging_console_only_by_default(_restore_root_logger, monkeypatch):
    monkeypatch.delenv("LOG_FILE", raising=False)
    setup_logging("DEBUG")
    root = logging.getLogger()
    assert root.level == logging.DEBUG
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0], logging.StreamHandler)
    assert isinstance(root.handlers[0].formatter, StructuredFormatter)


def test_setup_logging_adds_file_handler_from_env(_restore_root_logger, tmp_path, monkeypatch):
    log_path = tmp_path / "runtime.log"
    monkeypatch.setenv("LOG_FILE", str(log_path))
    setup_logging("INFO")
    root = logging.getLogger()
    assert len(root.handlers) == 2
    logging.getLogger("some.module").warning("file sink check")
    for h in root.handlers:
        h.flush()
    lines = [json.loads(line) for line in
             log_path.read_text(encoding="utf-8").splitlines() if line]
    assert any(body["msg"] == "file sink check" for body in lines)
