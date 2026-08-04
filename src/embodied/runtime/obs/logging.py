# Ported from car-agent observability/logging.py @ f0b08f8, changes: NatsLogHandler dropped (log shipping superseded by JSONL sink design); setup_structured_logging(service) replaced by setup_logging() with console + optional LOG_FILE file handler.
"""结构化日志 + 敏感字段脱敏。"""
from __future__ import annotations

import json
import logging
import os
import sys

from .redact import SENSITIVE_PATTERNS as _SHARED_PATTERNS


class StructuredFormatter(logging.Formatter):
    """结构化 JSON 日志格式。敏感字段自动脱敏（规则与观测事件共享 redact.py）。"""

    SENSITIVE_PATTERNS = _SHARED_PATTERNS

    def format(self, record):
        log_data = {
            "ts": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        # 附加 trace_id / session_id（如有）——badcase 排查按 id 直接 grep/检索
        from .tracing import get_session_id, get_trace_id
        tid = get_trace_id()
        if tid:
            log_data["trace_id"] = tid
        sid = get_session_id()
        if sid:
            log_data["session_id"] = sid

        # 附加额外字段
        if hasattr(record, "extra_data"):
            log_data.update(record.extra_data)

        text = json.dumps(log_data, ensure_ascii=False)
        return self._desensitize(text)

    def _desensitize(self, text: str) -> str:
        for pattern, replacement in self.SENSITIVE_PATTERNS:
            text = pattern.sub(replacement, text)
        return text


def setup_logging(level: str = "INFO", log_file: str | None = None) -> None:
    """配置结构化日志：stdout JSON 恒开；文件 handler 可选。

    log_file 参数缺省时读 env ``LOG_FILE``（非空即追加一个同格式的文件 handler）。
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    formatter = StructuredFormatter(datefmt="%Y-%m-%dT%H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    handlers: list[logging.Handler] = [console]

    path = log_file if log_file is not None else os.getenv("LOG_FILE", "")
    if path:
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)
    root.handlers = handlers

    # 降低吵闹库的日志级别
    for name in ("grpc", "urllib3", "httpx"):
        logging.getLogger(name).setLevel(logging.WARNING)

    logging.getLogger("obs.logging").info("Structured logging initialized at %s level", level)
