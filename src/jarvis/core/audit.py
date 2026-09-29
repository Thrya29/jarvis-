"""Append-only audit trail of every action JARVIS takes on the user's machine."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jarvis.core.logging_setup import redact


class AuditLog:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: str, **fields: Any) -> None:
        entry = {"ts": datetime.now(UTC).isoformat(), "event": event, **fields}
        line = redact(json.dumps(entry, default=str, ensure_ascii=False))
        with self._lock, self._path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
