"""Typed configuration.

Precedence (highest first): environment variables (``JARVIS_<SECTION>__<KEY>``),
the user's ``config.toml``, then the defaults below. Secrets (API keys) are never
stored here - see :mod:`jarvis.core.secrets`.
"""

from __future__ import annotations

import ipaddress
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import platformdirs
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


class Effort(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


class AnthropicConfig(BaseModel):
    model: str = "claude-opus-5-5"
    effort: Effort = Effort.HIGH
    # Requests are streamed, so a large output ceiling doesn't risk HTTP timeouts.
    max_tokens: int = Field(default=64_000, ge=1024, le=128_000)
    timeout_s: float = Field(default=600.0, gt=0)
    # Re-run a safety-classifier refusal on Anthropic's recommended fallback model.
    server_fallback: bool = True
    # Quick conversational replies and background work (cheaper, faster).
    fast_model: str = "claude-haiku-4-5"


class OllamaConfig(BaseModel):
    host: str = "http://127.0.0.1:11434"
    model: str = "qwen2.5:3b"
    timeout_s: float = Field(default=300.0, gt=0)
    num_ctx: int = Field(default=16_384, ge=2048)


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


class Activation(StrEnum):
    WAKE_WORD = "wake_word"  # say "Hey Jarvis" to start a conversation
    ALWAYS = "always"  # every utterance is for JARVIS (use in a quiet room)


VOICE_CHOICES = {"british_male", "british_female", "american_male", "american_female", "amy"}


class TTSEngine(StrEnum):
    PIPER = "piper"  # natural neural voice, runs locally
    SAPI = "sapi"  # Windows built-in voices, no download


class VoiceConfig(BaseModel):
    # Start the voice assistant with the daemon (`jarvis run`); `jarvis voice` always does.
    enabled: bool = False
    activation: Activation = Activation.WAKE_WORD
    wake_threshold: float = Field(default=0.5, gt=0.05, lt=1.0)
    stt_model: Literal["base.en", "small.en"] = "base.en"
    stt_threads: int = Field(default=0, ge=0, le=32, description="0 = automatic")
    tts_engine: TTSEngine = TTSEngine.PIPER
    tts_voice: str = "american_female"
    tts_speed: float = Field(default=1.0, ge=0.5, le=2.0)
    echo_cancellation: bool = True
    # Talking while JARVIS speaks pauses it at once; real speech then replaces its turn.
    barge_in: bool = True
    # After JARVIS finishes speaking, keep listening this long without the wake word.
    follow_up_s: float = Field(default=8.0, ge=0, le=120)
    vad_threshold: float = Field(default=0.5, gt=0.05, lt=1.0)
    end_silence_ms: int = Field(default=700, ge=200, le=3000)
    approval_timeout_s: float = Field(default=30.0, ge=5, le=300)
    input_device: int | str | None = None
    output_device: int | str | None = None

    @field_validator("tts_voice")
    @classmethod
    def _known_voice(cls, v: str) -> str:
        legacy = {"af_heart": "american_female", "lessac": "american_female"}
        v = legacy.get(v, v)
        if v not in VOICE_CHOICES:
            raise ValueError(f"tts_voice must be one of {sorted(VOICE_CHOICES)}")
        return v


# ----------------------------------------------------------------------------- user profile


class AddressMode(StrEnum):
    SIR = "sir"
    MAAM = "maam"
    NAME = "name"
    NICKNAME = "nickname"
    NONE = "none"


class ProfileConfig(BaseModel):
    name: str = Field(default="", max_length=60)
    address: AddressMode = AddressMode.NONE
    nickname: str = Field(default="", max_length=40)
    # Home location for weather and the maps; coordinates are looked up from the name.
    location: str = Field(default="", max_length=100)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    # Set once the user has been through the setup wizard.
    onboarded: bool = False

    def addressee(self) -> str | None:
        """How JARVIS should address the user, or None for no form of address."""
        if self.address is AddressMode.SIR:
            return "sir"
        if self.address is AddressMode.MAAM:
            return "ma'am"
        if self.address is AddressMode.NAME and self.name.strip():
            return self.name.strip().split()[0]
        if self.address is AddressMode.NICKNAME and self.nickname.strip():
            return self.nickname.strip()
        return None


class PersonaStyle(StrEnum):
    JARVIS = "jarvis"  # calm, precise, dry wit
    PROFESSIONAL = "professional"
    FRIENDLY = "friendly"
    MINIMAL = "minimal"


class PersonaConfig(BaseModel):
    style: PersonaStyle = PersonaStyle.JARVIS
    pushback: bool = True  # voice concerns before unwise actions
    progress_updates: bool = True  # speak short progress notes during long tasks
    quick_replies: bool = True  # answer small talk/questions with the fast model


# ----------------------------------------------------------------------------- features
# Choices made in the setup wizard. Features arriving in later versions are stored now
# and activate when that version is installed.


class DocumentsFeature(BaseModel):
    enabled: bool = False
    folders: list[Path] = Field(default_factory=list)


class EmailFeature(BaseModel):
    enabled: bool = False
    provider: Literal["microsoft", "google"] = "microsoft"
    account: Literal["personal", "work"] = "personal"


class ProtocolsFeature(BaseModel):
    enabled: bool = False
    briefing_time: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")


class MapsFeature(BaseModel):
    """Live map panels (v2.2). Each panel only contacts its own data source."""

    enabled: bool = False
    sky: bool = True  # aircraft overhead (OpenSky Network)
    network: bool = True  # who this PC is talking to (local + DB-IP lite database)
    situation: bool = True  # weather (Open-Meteo) and today's calendar
    sky_radius_km: int = Field(default=150, ge=20, le=400)


class HudFeature(BaseModel):
    enabled: bool = False
    dashboard: bool = False  # full dashboard on another monitor


class PhoneFeature(BaseModel):
    enabled: bool = False
    platform: Literal["android", "iphone"] = "android"


class SmartHomeFeature(BaseModel):
    enabled: bool = False
    url: str = ""  # Home Assistant address; its token lives in Credential Manager


class FeaturesConfig(BaseModel):
    web_research: bool = False
    documents: DocumentsFeature = DocumentsFeature()
    email: EmailFeature = EmailFeature()
    protocols: ProtocolsFeature = ProtocolsFeature()
    maps: MapsFeature = MapsFeature()
    hud: HudFeature = HudFeature()
    phone: PhoneFeature = PhoneFeature()
    smart_home: SmartHomeFeature = SmartHomeFeature()
    webcam: bool = False
    helpers: bool = False


class ConnectionsConfig(BaseModel):
    """App registrations used for "Sign in with Microsoft/Google".

    These identify the JARVIS app to the provider; they are not user secrets (desktop
    apps can't keep secrets, which is why sign-in uses PKCE). Tokens are never stored
    here: they live DPAPI-encrypted in the data folder. See docs/connections-setup.md.
    """

    microsoft_client_id: str = ""
    microsoft_tenant: str = Field(default="common", pattern=r"^[A-Za-z0-9.-]{1,64}$")
    google_client_id: str = ""
    google_client_secret: str = ""  # Google "Desktop app" clients issue a non-confidential one


class BudgetConfig(BaseModel):
    # Estimated API spend per day (USD); 0 means no limit.
    daily_usd: float = Field(default=5.0, ge=0, le=1000)
    # Which model background work (briefings, triggers, helpers) uses.
    background: Literal["fast", "main"] = "fast"


def _default_roots() -> list[Path]:
    # Known-folder lookups follow OneDrive redirection of Desktop/Documents. Only
    # folders that exist are used (e.g. some PCs have no Downloads folder).
    candidates = [
        Path(platformdirs.user_documents_dir()),
        Path(platformdirs.user_desktop_dir()),
        Path(platformdirs.user_downloads_dir()),
    ]
    existing = [p for p in candidates if p.is_dir()]
    return existing or [Path.home()]


class SafetyConfig(BaseModel):
    # File tools may only touch paths inside these folders.
    allowed_roots: list[Path] = Field(default_factory=_default_roots)
    # Creating new files inside allowed_roots without asking.
    auto_approve_writes: bool = True
    # Asking before running shell commands / code. Deleting, overwriting and anything
    # that leaves the machine (email, uploads) always asks regardless of this flag.
    confirm_risky_actions: bool = True
    kill_hotkey: str = "ctrl+alt+j"


class ScreenControl(StrEnum):
    ASK = "ask"  # ask once per task before JARVIS looks at or operates the screen
    ALLOW = "allow"
    DENY = "deny"


DEFAULT_BLOCKED_WINDOWS = [
    "*keepass*", "*1password*", "*bitwarden*", "*lastpass*", "*dashlane*", "*keeper*",
    "*proton pass*", "*nordpass*", "*roboform*", "windows security*", "*credential manager*",
]  # fmt: skip


class DesktopConfig(BaseModel):
    screen_control: ScreenControl = ScreenControl.ASK
    monitor: int = Field(default=1, ge=1, description="1 = primary monitor")
    # Screenshots are downscaled to this long edge before the model sees them.
    max_screenshot_edge: int = Field(default=1366, ge=640, le=2000)
    # Case-insensitive glob patterns; JARVIS won't read or operate matching windows.
    blocked_windows: list[str] = Field(default_factory=lambda: list(DEFAULT_BLOCKED_WINDOWS))


class MemoryConfig(BaseModel):
    enabled: bool = True
    # Ask before saving or changing a memory or workflow, so nothing is remembered
    # without the user seeing it (also blunts prompt injection that tries to plant memories).
    confirm_writes: bool = True
    # Memories shown to the model with each goal (all preferences + best matches).
    context_items: int = Field(default=12, ge=0, le=50)


class AgentConfig(BaseModel):
    max_turns: int = Field(default=60, ge=1, le=500)
    task_timeout_s: float = Field(default=1800.0, gt=0)
    tool_timeout_s: float = Field(default=120.0, gt=0)
    max_tool_output_chars: int = Field(default=30_000, ge=1000)


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
    agent: AgentConfig = AgentConfig()
    desktop: DesktopConfig = DesktopConfig()
    memory: MemoryConfig = MemoryConfig()
    profile: ProfileConfig = ProfileConfig()
    persona: PersonaConfig = PersonaConfig()
    features: FeaturesConfig = FeaturesConfig()
    budget: BudgetConfig = BudgetConfig()
    connections: ConnectionsConfig = ConnectionsConfig()
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
    # TOML has no null: unset optional values (e.g. audio devices) are simply omitted.
    data: dict[str, Any] = Settings.model_construct().model_dump(mode="json", exclude_none=True)
    target.write_text(tomli_w.dumps(data), encoding="utf-8")
    return target
