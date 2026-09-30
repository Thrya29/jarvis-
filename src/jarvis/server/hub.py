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
import tomllib
from collections.abc import Awaitable, Callable
from typing import Any

import tomli_w

from jarvis.agent.events import Event, EventSink
from jarvis.agent.loop import Agent
from jarvis.core.config import LLMProvider as ProviderName
from jarvis.core.config import Settings
from jarvis.core.paths import AppPaths
from jarvis.core.secrets import SecretName, get_secret, set_secret
from jarvis.llm import LLMError, create_provider
from jarvis.llm.base import LLMProvider
from jarvis.memory.store import Store
from jarvis.safety.policy import Approver
from jarvis.server.session import TaskBoard

log = logging.getLogger(__name__)

Broadcast = Callable[[Event], Awaitable[None]]


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


class Hub:
    def __init__(self, settings: Settings, paths: AppPaths) -> None:
        self.settings = settings
        self.paths = paths
        self.board = TaskBoard()
        self.generation = 0
        self._provider: LLMProvider | None = None
        self._store: Store | None = None
        self._broadcast: Broadcast | None = None
        self._voice_task: asyncio.Task[None] | None = None
        self.voice_state = "off"
        self.setup_progress: dict[str, Any] | None = None
        self._background: set[asyncio.Future[Any]] = set()
        self.loop: asyncio.AbstractEventLoop | None = None  # set at startup (tray uses it)

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
    def store(self) -> Store | None:
        if self._store is None and self.settings.memory.enabled:
            from jarvis.agent.factory import open_store

            self._store = open_store(self.settings, self.paths)
        return self._store

    # ------------------------------------------------------------------ agents

    def provider(self) -> LLMProvider:
        if self._provider is None:
            self._provider = create_provider(self.settings.llm)
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
        }

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

    # ------------------------------------------------------------------ lifecycle

    async def startup(self) -> None:
        self.loop = asyncio.get_running_loop()
        if self.settings.voice.enabled:
            try:
                self.provider()
            except LLMError as exc:
                log.warning("voice not started: %s", exc)
                return
            await self.start_voice()

    async def shutdown(self) -> None:
        await self.stop_voice()
        if self._provider is not None:
            with contextlib.suppress(Exception):
                await self._provider.aclose()
        if self._store is not None:
            self._store.close()
