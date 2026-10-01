"""Encrypted storage for connection tokens.

OAuth refresh tokens are too large for Windows Credential Manager entries, so they're
kept in files encrypted with DPAPI (``CryptProtectData``), bound to the current Windows
user: another user, or the same file copied to another PC, can't decrypt them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ENTROPY = b"jarvis-connections-v1"


def _protect(data: bytes) -> bytes:
    if sys.platform != "win32":  # pragma: no cover - JARVIS ships for Windows
        raise OSError("encrypted token storage requires Windows")
    import win32crypt

    blob: bytes = win32crypt.CryptProtectData(data, "jarvis", ENTROPY, None, None, 0)
    return blob


def _unprotect(blob: bytes) -> bytes:
    if sys.platform != "win32":  # pragma: no cover
        raise OSError("encrypted token storage requires Windows")
    import win32crypt

    _, data = win32crypt.CryptUnprotectData(blob, ENTROPY, None, None, 0)
    return bytes(data)


class SecureStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        if not name.replace("-", "").replace("_", "").isalnum():
            raise ValueError("invalid secure-store key")
        return self.root / f"{name}.bin"

    def save(self, name: str, value: dict[str, Any]) -> None:
        path = self._path(name)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(_protect(json.dumps(value).encode("utf-8")))
        tmp.replace(path)

    def load(self, name: str) -> dict[str, Any] | None:
        path = self._path(name)
        if not path.exists():
            return None
        try:
            value: dict[str, Any] = json.loads(_unprotect(path.read_bytes()))
        except (OSError, ValueError):
            return None  # unreadable (other user / other PC): treat as not connected
        return value

    def delete(self, name: str) -> bool:
        path = self._path(name)
        if path.exists():
            path.unlink()
            return True
        return False
