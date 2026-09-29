"""Typed configuration.

Precedence (highest first): environment variables (``JARVIS_<SECTION>__<KEY>``),
the user's ``config.toml``, then the defaults below. Secrets (API keys) are never
stored here - see :mod:`jarvis.core.secrets`.
"""

from __future__ import annotations

import ipaddress
from enum import StrEnum
from pathlib import Path
from typing import Any

import tomli_w
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from jarvis.core.paths import get_paths

CONFIG_SCHEMA_VERSION = 1


class LLMProvider(StrEnum):
    ANTHROPIC = "anthropic"
    OLLAMA = "ollama"


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class AnthropicConfig(BaseModel):
    model: str = "claude-sonnet-5-5"
    max_tokens: int = Field(default=8192, ge=256, le=128_000)
    timeout_s: float = Field(default=120.0, gt=0)


class OllamaConfig(BaseModel):
    host: str = "http://127.0.0.1:11434"
    model: str = "qwen2.5:3b"
    timeout_s: float = Field(default=300.0, gt=0)


class LLMConfig(BaseModel):
    provider: LLMProvider = LLMProvider.ANTHROPIC
    anthropic: AnthropicConfig = AnthropicConfig()
    ollama: OllamaConfig = OllamaConfig()


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1024, le=65535)

    @field_validator("host")
    @classmethod
    def _loopback_only(cls, v: str) -> str:
        # The control API can drive the whole desktop; it must never be reachable off-box.
        if v == "localhost":
            return "127.0.0.1"
        if not ipaddress.ip_address(v).is_loopback:
            raise ValueError("server.host must be a loopback address")
        return v


class VoiceConfig(BaseModel):
    enabled: bool = False
    wake_word: str = "hey_jarvis"
    stt_model: str = "base.en"
    tts_voice: str = "af_heart"


class SafetyConfig(BaseModel):
    allowed_roots: list[Path] = Field(default_factory=lambda: [Path.home() / "Documents"])
    confirm_risky_actions: bool = True
    kill_hotkey: str = "ctrl+alt+j"


class LoggingConfig(BaseModel):
    level: LogLevel = LogLevel.INFO
    json_format: bool = True
    max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    backup_count: int = Field(default=5, ge=0)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="JARVIS_",
        env_nested_delimiter="__",
        extra="forbid",
    )

    schema_version: int = CONFIG_SCHEMA_VERSION
    llm: LLMConfig = LLMConfig()
    server: ServerConfig = ServerConfig()
    voice: VoiceConfig = VoiceConfig()
    safety: SafetyConfig = SafetyConfig()
    logging: LoggingConfig = LoggingConfig()

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        toml_file = get_paths().config_file
        return (
            init_settings,
            env_settings,
            TomlConfigSettingsSource(settings_cls, toml_file=toml_file),
        )

    @field_validator("schema_version")
    @classmethod
    def _known_schema(cls, v: int) -> int:
        if v > CONFIG_SCHEMA_VERSION:
            raise ValueError(
                f"config schema_version {v} is newer than this build supports "
                f"({CONFIG_SCHEMA_VERSION}); upgrade JARVIS"
            )
        return v


def load_settings() -> Settings:
    return Settings()


def write_default_config(path: Path | None = None, *, overwrite: bool = False) -> Path:
    """Write a config.toml populated with defaults. Returns the path written."""
    target = path or get_paths().config_file
    if target.exists() and not overwrite:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = Settings.model_construct().model_dump(mode="json")
    target.write_text(tomli_w.dumps(data), encoding="utf-8")
    return target
