"""Structured logging with rotation and secret redaction."""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jarvis.core.config import LoggingConfig

_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(
        r"(?i)(authorization|x-api-key|token)([\"':= ]+(?:bearer\s+)?)([A-Za-z0-9_\-\.]{12,})"
    ),
]

_STD_ATTRS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "color_message"}


def redact(text: str) -> str:
    text = _SECRET_PATTERNS[0].sub("sk-ant-***", text)
    return _SECRET_PATTERNS[1].sub(r"\1\2***", text)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key not in _STD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str, ensure_ascii=False)


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def configure_logging(cfg: LoggingConfig, log_dir: Path, *, console: bool = True) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(cfg.level.value)

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "jarvis.log",
        maxBytes=cfg.max_bytes,
        backupCount=cfg.backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(
        JsonFormatter()
        if cfg.json_format
        else RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root.addHandler(file_handler)

    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(RedactingFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        root.addHandler(stream)

    logging.captureWarnings(True)
