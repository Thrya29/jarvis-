from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import keyring
import keyring.backend
import keyring.errors
import pytest

from jarvis.core.paths import AppPaths, get_paths


class MemoryKeyring(keyring.backend.KeyringBackend):
    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.store:
            raise keyring.errors.PasswordDeleteError("not set")
        del self.store[(service, username)]


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every test gets its own JARVIS_HOME, a clean environment and an in-memory keyring."""
    for key in list(os.environ):
        if key.startswith("JARVIS_") or key == "ANTHROPIC_API_KEY":
            monkeypatch.delenv(key)
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    previous = keyring.get_keyring()
    keyring.set_keyring(MemoryKeyring())
    yield
    keyring.set_keyring(previous)


@pytest.fixture
def paths() -> AppPaths:
    return get_paths().ensure()
