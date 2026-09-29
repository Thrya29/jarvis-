from __future__ import annotations

import pytest
from pydantic import ValidationError

from jarvis.core.config import LLMProvider, Settings, load_settings, write_default_config
from jarvis.core.paths import AppPaths


def test_defaults() -> None:
    s = load_settings()
    assert s.llm.provider is LLMProvider.ANTHROPIC
    assert s.server.host == "127.0.0.1"


def test_toml_file_is_read(paths: AppPaths) -> None:
    paths.config_file.write_text('[llm]\nprovider = "ollama"\n', encoding="utf-8")
    assert load_settings().llm.provider is LLMProvider.OLLAMA


def test_env_overrides_toml(paths: AppPaths, monkeypatch: pytest.MonkeyPatch) -> None:
    paths.config_file.write_text('[llm]\nprovider = "ollama"\n', encoding="utf-8")
    monkeypatch.setenv("JARVIS_LLM__PROVIDER", "anthropic")
    assert load_settings().llm.provider is LLMProvider.ANTHROPIC


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "::"])  # noqa: S104
def test_server_rejects_non_loopback(host: str) -> None:
    with pytest.raises(ValidationError):
        Settings(server={"host": host})  # type: ignore[arg-type]


def test_localhost_normalised() -> None:
    assert Settings(server={"host": "localhost"}).server.host == "127.0.0.1"  # type: ignore[arg-type]


def test_unknown_keys_rejected(paths: AppPaths) -> None:
    paths.config_file.write_text("typo_section = 1\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_settings()


def test_future_schema_rejected(paths: AppPaths) -> None:
    paths.config_file.write_text("schema_version = 99\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_settings()


def test_write_default_config_roundtrips(paths: AppPaths) -> None:
    target = write_default_config(paths.config_file)
    assert target.exists()
    assert load_settings() == Settings()


def test_write_default_config_keeps_existing(paths: AppPaths) -> None:
    paths.config_file.write_text('[llm]\nprovider = "ollama"\n', encoding="utf-8")
    write_default_config(paths.config_file)
    assert load_settings().llm.provider is LLMProvider.OLLAMA
