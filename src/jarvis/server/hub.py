"""Daemon-wide state shared by the API, the desktop UI and voice.

- A lazily created LLM provider, so the app can start before an API key exists and
  pick one up after first-run setup (``generation`` tells sessions to rebuild agents).
- Broadcasting events to every connected UI (voice tasks, setup progress, voice state).
- Starting/stopping the voice assistant at runtime.
- Persisting settings changed from the UI to config.toml.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import tomllib
from collections.abc import Awaitable, Callable
from typing import Any

import tomli_w
from pydantic import BaseModel

from jarvis.agent.events import Event, EventSink
from jarvis.agent.loop import Agent
from jarvis.connect.accounts import (
    Account,
    AccountError,
    Capability,
    ConnectionManager,
    Provider,
)
from jarvis.connect.imap import ImapSettings
from jarvis.core.config import (
    BudgetConfig,
    ConnectionsConfig,
    FeaturesConfig,
    PersonaConfig,
    ProfileConfig,
    Settings,
    VoiceConfig,
)
from jarvis.core.config import LLMProvider as ProviderName
from jarvis.core.paths import AppPaths
from jarvis.core.secrets import SecretName, get_secret, set_secret
from jarvis.llm import LLMError, create_provider
from jarvis.llm.base import LLMProvider
from jarvis.llm.budget import SpendMeter
from jarvis.memory.store import Store
from jarvis.safety.policy import Approver
from jarvis.server.session import TaskBoard
from jarvis.voice.models import PIPER_VOICES

log = logging.getLogger(__name__)

Broadcast = Callable[[Event], Awaitable[None]]
DOC_SYNC_EVERY_S = 30 * 60


def update_config_file(paths: AppPaths, changes: dict[str, dict[str, Any]]) -> None:
    """Merge {section: {key: value}} into config.toml, keeping everything else."""
    path = paths.config_file
    data: dict[str, Any] = {}
    if path.exists():
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    for section, values in changes.items():
        node = data
        for part in section.split("."):
            node = node.setdefault(part, {})
        node.update(values)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(tomli_w.dumps(data), encoding="utf-8")
    tmp.replace(path)


def _deep_merge(base: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _play(audio: Any, device: int | str | None) -> None:
    import sounddevice as sd

    from jarvis.voice.audio import OUT_SR

    sd.play(audio, OUT_SR, device=device)
    sd.wait()


class Hub:
    def __init__(self, settings: Settings, paths: AppPaths) -> None:
        self.settings = settings
        self.paths = paths
        self.board = TaskBoard()
        self.generation = 0
        self._provider: LLMProvider | None = None
        self._store: Store | None = None
        self._meter: SpendMeter | None = None
        self._broadcast: Broadcast | None = None
        self._voice_task: asyncio.Task[None] | None = None
        self.voice_state = "off"
        self.setup_progress: dict[str, Any] | None = None
        self._background: set[asyncio.Future[Any]] = set()
        self.loop: asyncio.AbstractEventLoop | None = None  # set at startup (tray uses it)
        self._signin: asyncio.Task[Account] | None = None
        self.docs_state: dict[str, Any] = {"state": "idle"}
        self._doc_lock = asyncio.Lock()

    def _spawn(self, coro: Awaitable[Any]) -> None:
        """Fire-and-forget on the event loop, keeping a reference until it finishes."""
        fut = asyncio.ensure_future(coro)
        self._background.add(fut)
        fut.add_done_callback(self._background.discard)

    # ------------------------------------------------------------------ plumbing

    def attach_broadcast(self, broadcast: Broadcast) -> None:
        self._broadcast = broadcast

    async def broadcast(self, event: Event) -> None:
        if self._broadcast is not None:
            await self._broadcast(event)

    @property
    def meter(self) -> SpendMeter:
        if self._meter is None:
            from jarvis.agent.factory import open_meter

            self._meter = open_meter(self.settings, self.paths)
        return self._meter

    @property
    def store(self) -> Store | None:
        if self._store is None and self.settings.memory.enabled:
            from jarvis.agent.factory import open_store

            self._store = open_store(self.settings, self.paths)
        return self._store

    @property
    def connections(self) -> ConnectionManager:
        from jarvis.agent.factory import open_connections

        return open_connections(self.settings, self.paths)

    # ------------------------------------------------------------------ agents

    def provider(self) -> LLMProvider:
        if self._provider is None:
            self._provider = create_provider(self.settings.llm, self.meter)
        return self._provider

    def agent_factory(self, approver: Approver, emit: EventSink) -> Agent:
        """Raises LLMError when setup isn't complete (no key / Ollama unreachable)."""
        from jarvis.agent.factory import build_agent

        agent, _ = build_agent(
            self.settings, self.paths, approver, emit, provider=self.provider(), store=self.store
        )
        return agent

    async def reset_provider(self) -> None:
        old, self._provider = self._provider, None
        self.generation += 1
        if old is not None:
            with contextlib.suppress(Exception):
                await old.aclose()

    def setup_status(self) -> dict[str, Any]:
        from jarvis.voice.runtime import model_store, required_files

        llm = self.settings.llm
        try:
            voice_ready = not model_store(self.paths).missing(required_files(self.settings))
        except Exception:
            voice_ready = False
        key_set = get_secret(SecretName.ANTHROPIC_API_KEY) is not None
        return {
            "provider": llm.provider.value,
            "model": llm.anthropic.model
            if llm.provider is ProviderName.ANTHROPIC
            else llm.ollama.model,
            "api_key_set": key_set,
            "llm_ready": key_set or llm.provider is ProviderName.OLLAMA,
            "voice_models_ready": voice_ready,
            "voice_enabled": self.settings.voice.enabled,
            "voice_state": self.voice_state,
            "screen_control": self.settings.desktop.screen_control.value,
            "allowed_roots": [str(p) for p in self.settings.safety.allowed_roots],
            "onboarded": self.settings.profile.onboarded,
            "spend_today_usd": round(self.meter.today_usd(), 4),
            "daily_cap_usd": self.settings.budget.daily_usd,
            "documents": self.documents_status(),
            "connected_accounts": len(self.connections.accounts()),
        }

    # ------------------------------------------------------------------ settings

    def settings_view(self) -> dict[str, Any]:
        s = self.settings
        return {
            "profile": s.profile.model_dump(mode="json"),
            "persona": s.persona.model_dump(mode="json"),
            "voice": {
                "tts_voice": s.voice.tts_voice,
                "tts_speed": s.voice.tts_speed,
                "activation": s.voice.activation.value,
            },
            "features": s.features.model_dump(mode="json"),
            "budget": s.budget.model_dump(mode="json"),
            "connections": s.connections.model_dump(mode="json"),
            "voices": sorted(k for k in PIPER_VOICES if k != "amy"),
        }

    async def update_settings(self, changes: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """Validate, apply and persist settings changes from the UI.

        Raises ValueError (pydantic ValidationError) on invalid values; nothing is
        written unless every section validates.
        """
        models: dict[str, type[BaseModel]] = {
            "profile": ProfileConfig,
            "persona": PersonaConfig,
            "features": FeaturesConfig,
            "budget": BudgetConfig,
            "connections": ConnectionsConfig,
        }
        validated: dict[str, BaseModel] = {}
        for section, values in changes.items():
            if section == "voice":
                allowed = {"tts_voice", "tts_speed", "activation"}
                extra = set(values) - allowed
                if extra:
                    raise ValueError(f"voice settings not editable here: {sorted(extra)}")
                current = self.settings.voice.model_dump(mode="json")
                validated[section] = VoiceConfig(**{**current, **values})
            elif section in models:
                current = getattr(self.settings, section).model_dump(mode="json")
                validated[section] = models[section](**_deep_merge(current, values))
            else:
                raise ValueError(f"unknown settings section {section!r}")

        for section, model in validated.items():
            data = model.model_dump(mode="json", exclude_none=True)
            if section == "voice":
                data = {k: data[k] for k in ("tts_voice", "tts_speed", "activation")}
            update_config_file(self.paths, {section: data})
            # Update in place: providers and meters hold references to these objects.
            target = getattr(self.settings, section)
            for field in type(model).model_fields:
                setattr(target, field, getattr(model, field))

        if validated:
            self.generation += 1  # sessions rebuild agents with the new persona/profile
            if "features" in validated and self.settings.features.documents.enabled:
                self._spawn(self.index_documents())
            if self.voice_running and set(validated) & {"voice", "persona", "profile"}:
                await self.stop_voice()
                await self.start_voice()
        return self.settings_view()

    async def preview_voice(self, voice: str, speed: float) -> None:
        """Say a short sample in the chosen voice (downloading it first if needed)."""
        from jarvis.voice.models import piper_files
        from jarvis.voice.runtime import model_store
        from jarvis.voice.speech import PiperTTS

        VoiceConfig(tts_voice=voice, tts_speed=speed)  # validate
        store = model_store(self.paths)
        files = piper_files(voice)
        if store.missing(files):
            loop = asyncio.get_running_loop()

            def progress(name: str, done: int, total: int) -> None:
                event = {"type": "setup.progress", "file": name, "done": done, "total": total}
                loop.call_soon_threadsafe(lambda: self._spawn(self.broadcast(event)))

            await asyncio.to_thread(store.fetch, files, progress)
        who = self.settings.profile.addressee()
        greeting = f"Hello, {who}." if who else "Hello."
        tts = PiperTTS(store.path(files[0]), speed)
        try:
            audio = await tts.synthesize(f"{greeting} This is how I will sound. All systems ready.")
        finally:
            tts.close()
        await asyncio.to_thread(_play, audio, self.settings.voice.output_device)

    async def set_api_key(self, key: str) -> None:
        """Validate the key against the API before storing it."""
        from jarvis.llm.anthropic_provider import AnthropicProvider

        key = key.strip()
        if not key.startswith("sk-ant-") or len(key) < 30:
            raise LLMError("that doesn't look like an Anthropic API key (it starts with sk-ant-)")
        probe = AnthropicProvider(self.settings.llm.anthropic, key)
        try:
            ok, detail = await probe.check()
        finally:
            await probe.aclose()
        if not ok:
            raise LLMError(f"the key didn't work: {detail}")
        set_secret(SecretName.ANTHROPIC_API_KEY, key)
        await self.use_provider(ProviderName.ANTHROPIC)

    async def use_provider(self, provider: ProviderName, ollama_model: str | None = None) -> None:
        changes: dict[str, Any] = {"provider": provider.value}
        update_config_file(self.paths, {"llm": changes})
        self.settings.llm.provider = provider
        if ollama_model:
            update_config_file(self.paths, {"llm.ollama": {"model": ollama_model}})
            self.settings.llm.ollama.model = ollama_model
        await self.reset_provider()
        if self._voice_task is not None:
            await self.stop_voice()
            await self.start_voice()

    # ------------------------------------------------------------------ voice

    @property
    def voice_running(self) -> bool:
        return self._voice_task is not None and not self._voice_task.done()

    async def _set_voice_state(self, state: str) -> None:
        self.voice_state = state
        await self.broadcast({"type": "voice.state", "state": state})

    async def start_voice(self) -> None:
        if self.voice_running:
            return
        from jarvis.voice.runtime import voice_session

        loop = asyncio.get_running_loop()

        def log_line(text: str) -> None:
            text = text.strip()
            if text:
                event = {"type": "voice.log", "text": text}
                loop.call_soon_threadsafe(lambda: self._spawn(self.broadcast(event)))

        async def voice_sink(event: Event) -> None:
            await self.broadcast({**event, "source": "voice"})

        async def run() -> None:
            await self._set_voice_state("starting")
            try:
                await voice_session(
                    self.settings,
                    self.paths,
                    self.provider(),
                    log_line,
                    self.board,
                    voice_sink,
                    on_ready=lambda rt: self._spawn(self._set_voice_state("listening")),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("voice assistant stopped")
                await self.broadcast({"type": "error", "error": f"Voice stopped: {exc}"})
            finally:
                await self._set_voice_state("off")

        self._voice_task = asyncio.create_task(run())

    async def stop_voice(self) -> None:
        task, self._voice_task = self._voice_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def set_voice_enabled(self, enabled: bool) -> None:
        update_config_file(self.paths, {"voice": {"enabled": enabled}})
        self.settings.voice.enabled = enabled
        if enabled:
            await self.start_voice()
        else:
            await self.stop_voice()

    async def download_voice_models(self) -> None:
        from jarvis.voice.runtime import setup_models

        loop = asyncio.get_running_loop()

        def progress(name: str, done: int, total: int) -> None:
            self.setup_progress = {"file": name, "done": done, "total": total}
            event = {"type": "setup.progress", **self.setup_progress}
            loop.call_soon_threadsafe(lambda: self._spawn(self.broadcast(event)))

        try:
            await asyncio.to_thread(setup_models, self.settings, self.paths, progress)
            await self.broadcast({"type": "setup.voice_ready"})
        except Exception as exc:
            log.exception("voice model download failed")
            await self.broadcast({"type": "setup.error", "error": str(exc)})
        finally:
            self.setup_progress = None

    # ------------------------------------------------------------------ connections

    def connections_status(self) -> dict[str, Any]:
        status = self.connections.status()
        task = self._signin
        # The sign-in task itself reports its result, by which point it's finished.
        status["signing_in"] = (
            task is not None and not task.done() and task is not asyncio.current_task()
        )
        return status

    async def _connections_changed(self) -> None:
        self.generation += 1  # agents rebuild with the new tools and account list
        await self.broadcast({"type": "connections.changed", **self.connections_status()})

    async def start_sign_in(self, provider: Provider, capabilities: list[Capability]) -> None:
        """Open the provider's sign-in page; the result arrives as a broadcast event."""
        if self._signin is not None and not self._signin.done():
            raise AccountError("a sign-in is already in progress; finish or cancel it first")
        manager = self.connections
        manager.check(provider, capabilities)  # fail fast on bad input / missing app ID

        async def run() -> Account:
            try:
                account = await manager.connect(provider, capabilities)
            except asyncio.CancelledError:
                await self.broadcast({"type": "connections.error", "error": "sign-in cancelled"})
                raise
            except AccountError as exc:
                await self.broadcast({"type": "connections.error", "error": str(exc)})
                raise
            await self._connections_changed()
            return account

        self._signin = asyncio.ensure_future(run())
        self._signin.add_done_callback(lambda t: t.cancelled() or t.exception())

    async def cancel_sign_in(self) -> bool:
        if self._signin is None or self._signin.done():
            return False
        self._signin.cancel()
        with contextlib.suppress(asyncio.CancelledError, AccountError):
            await self._signin
        return True

    async def connect_imap(
        self, settings: ImapSettings, password: str, capabilities: list[Capability]
    ) -> Account:
        account = await self.connections.connect_imap(settings, password, capabilities)
        await self._connections_changed()
        return account

    async def disconnect(self, account_id: str) -> bool:
        removed = await self.connections.disconnect(account_id)
        if removed:
            await self._connections_changed()
        return removed

    # ------------------------------------------------------------------ documents

    def documents_status(self) -> dict[str, Any]:
        from jarvis.agent.factory import open_documents
        from jarvis.voice.runtime import document_files, model_store

        docs = self.settings.features.documents
        out: dict[str, Any] = {"enabled": docs.enabled, **self.docs_state}
        if not docs.enabled:
            return out
        try:
            out["model_ready"] = not model_store(self.paths).missing(document_files(self.settings))
            index = open_documents(self.settings, self.paths) if out["model_ready"] else None
            if index is not None:
                out.update(index.stats())
        except Exception as exc:  # status must never break the UI
            out["error"] = str(exc)
        return out

    async def index_documents(self) -> None:
        """Download the model if needed, then bring the index up to date."""
        from jarvis.agent.factory import open_documents
        from jarvis.safety.policy import PathGuard
        from jarvis.voice.runtime import setup_document_models

        if not self.settings.features.documents.enabled or self._doc_lock.locked():
            return
        loop = asyncio.get_running_loop()

        def emit(state: dict[str, Any]) -> None:
            self.docs_state = state
            event = {"type": "documents.progress", **state}
            loop.call_soon_threadsafe(lambda: self._spawn(self.broadcast(event)))

        async with self._doc_lock:
            try:
                emit({"state": "downloading"})
                await asyncio.to_thread(
                    setup_document_models,
                    self.settings,
                    self.paths,
                    lambda name, done, total: emit(
                        {"state": "downloading", "done": done, "total": total}
                    ),
                )
                index = open_documents(self.settings, self.paths)
                if index is None:
                    return
                docs = self.settings.features.documents
                guard = PathGuard(
                    self.settings.safety.allowed_roots,
                    deny_roots=[self.paths.config_dir, self.paths.data_dir, self.paths.log_dir],
                )
                folders = docs.folders or list(guard.allowed)
                emit({"state": "indexing"})
                report = await asyncio.to_thread(
                    index.sync,
                    folders,
                    guard,
                    3600.0,
                    lambda done, total: emit({"state": "indexing", "done": done, "total": total}),
                )
                emit(
                    {
                        "state": "idle",
                        "indexed": report.indexed,
                        "skipped_folders": [str(f) for f in report.skipped_folders],
                        "finished_at": time.time(),
                    }
                )
            except Exception as exc:
                log.exception("document indexing failed")
                emit({"state": "error", "error": str(exc)})

    async def _documents_loop(self) -> None:
        while True:
            await self.index_documents()
            await asyncio.sleep(DOC_SYNC_EVERY_S)

    # ------------------------------------------------------------------ lifecycle

    async def startup(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._spawn(self._documents_loop())
        if self.settings.voice.enabled:
            try:
                self.provider()
            except LLMError as exc:
                log.warning("voice not started: %s", exc)
                return
            await self.start_voice()

    async def shutdown(self) -> None:
        await self.cancel_sign_in()
        for fut in list(self._background):
            fut.cancel()
        await self.stop_voice()
        if self._provider is not None:
            with contextlib.suppress(Exception):
                await self._provider.aclose()
        if self._store is not None:
            self._store.close()
