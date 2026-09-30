"""Assemble and run the voice assistant."""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from jarvis.core.config import Activation, Settings, TTSEngine
from jarvis.core.paths import AppPaths
from jarvis.voice.models import WAKE_FILES, ModelStore, piper_files

log = logging.getLogger(__name__)


def model_store(paths: AppPaths) -> ModelStore:
    return ModelStore(paths.cache_dir / "models")


def required_files(settings: Settings) -> list[str]:
    v = settings.voice
    files: list[str] = []
    if v.activation is Activation.WAKE_WORD:
        files += WAKE_FILES
    if v.tts_engine is TTSEngine.PIPER:
        files += piper_files(v.tts_voice)
    return files


def setup_models(
    settings: Settings, paths: AppPaths, progress: Callable[[str, int, int], None] | None = None
) -> None:
    """Download and verify everything voice needs (idempotent)."""
    from jarvis.voice.speech import Transcriber

    store = model_store(paths)
    store.fetch(required_files(settings), progress)
    Transcriber(settings.voice.stt_model, store.whisper_dir(), settings.voice.stt_threads).load()


@dataclass
class VoiceRuntime:
    assistant: Any
    devices: Any
    stt: Any
    tts: Any
    uses_aec: bool

    def stop(self) -> None:
        self.devices.stop()
        self.stt.close()
        self.tts.close()


def build_voice(
    settings: Settings, paths: AppPaths, log_line: Callable[[str], None]
) -> VoiceRuntime:
    """Build every component. Raises ModelError if models are missing (run `jarvis voice setup`)."""
    from jarvis.voice.audio import AudioDevices, EchoCanceller, Frontend, Player
    from jarvis.voice.engine import VoiceAssistant
    from jarvis.voice.models import ModelError
    from jarvis.voice.speech import PiperTTS, SapiTTS, Transcriber, tone
    from jarvis.voice.vad import Segmenter, SegmenterConfig, SileroVAD
    from jarvis.voice.wake import WakeWordDetector

    v = settings.voice
    store = model_store(paths)
    missing = store.missing(required_files(settings))
    if missing:
        raise ModelError(f"voice models missing ({', '.join(missing)}) - run `jarvis voice setup`")

    stt = Transcriber(v.stt_model, store.whisper_dir(), v.stt_threads)
    tts: Any
    if v.tts_engine is TTSEngine.PIPER:
        tts = PiperTTS(store.path(piper_files(v.tts_voice)[0]), v.tts_speed)
    else:
        tts = SapiTTS(v.tts_speed)

    holder: dict[str, VoiceAssistant] = {}
    player = Player(on_idle=lambda: holder["va"].player_idle())
    assistant = VoiceAssistant(
        v,
        stt,
        tts,
        player,
        chime=tone(),
        log_line=log_line,
        speak_progress=settings.persona.progress_updates,
        addressee=settings.profile.addressee(),
    )
    holder["va"] = assistant

    wake = None
    if v.activation is Activation.WAKE_WORD:
        wake = WakeWordDetector(
            store.path("melspectrogram.onnx"),
            store.path("embedding_model.onnx"),
            store.path("hey_jarvis_v0.1.onnx"),
            v.wake_threshold,
        )
    aec = EchoCanceller(v.echo_cancellation)
    segmenter = Segmenter(
        SegmenterConfig(
            threshold=v.vad_threshold,
            neg_threshold=max(0.05, v.vad_threshold - 0.15),
            end_silence_ms=v.end_silence_ms,
        )
    )
    frontend = Frontend(assistant.post, aec, segmenter, SileroVAD(), wake, player, time.monotonic)
    devices = AudioDevices(frontend, player, aec, v.input_device, v.output_device)
    return VoiceRuntime(assistant, devices, stt, tts, aec.active)


async def voice_session(
    settings: Settings,
    paths: AppPaths,
    provider: Any,
    log_line: Callable[[str], None],
    board: Any | None = None,
    extra_sink: Callable[[dict[str, Any]], Any] | None = None,
    on_ready: Callable[[VoiceRuntime], object] | None = None,
) -> None:
    """Run a voice session until cancelled. Shared by `jarvis voice` and the daemon."""
    from jarvis.agent.factory import build_agent
    from jarvis.voice.engine import combine_sinks

    rt = build_voice(settings, paths, log_line)
    va = rt.assistant
    if board is not None:
        va._task_lock = board.lock
        va._track = board.track
        board.on_kill(va.silence)
    sinks = [va.on_agent_event] + ([extra_sink] if extra_sink else [])
    agent, _ = build_agent(settings, paths, va.approver, combine_sinks(*sinks), provider=provider)
    va.attach(agent)
    try:
        await rt.stt.warm_up()
        rt.devices.start()
        if on_ready:
            on_ready(rt)
        await va.run()
    finally:
        rt.stop()
        agent.close()


async def run_voice(settings: Settings, paths: AppPaths) -> int:
    """`jarvis voice`: a hands-free session in this terminal."""
    from jarvis.interfaces.console import print_event
    from jarvis.llm import LLMError, create_provider
    from jarvis.server.session import TaskBoard
    from jarvis.voice.models import ModelError

    def log_line(text: str) -> None:
        print(text, flush=True)

    try:
        from jarvis.agent.factory import open_meter

        provider = create_provider(settings.llm, open_meter(settings, paths))
    except LLMError as exc:
        print(exc, file=sys.stderr)
        return 2
    board = TaskBoard()
    loop = asyncio.get_running_loop()
    switch = None
    if sys.platform == "win32":
        from jarvis.desktop.killswitch import KillSwitch

        def on_kill() -> None:
            if board.cancel_all():
                log_line("  [kill switch] stopped")

        switch = KillSwitch(settings.safety.kill_hotkey, lambda: loop.call_soon_threadsafe(on_kill))
        switch.start()

    def ready(rt: VoiceRuntime) -> None:
        how = (
            'Say "Hey Jarvis" and then your request.'
            if settings.voice.activation is Activation.WAKE_WORD
            else "Just talk - every utterance goes to JARVIS."
        )
        log_line(
            f"JARVIS is listening ({provider.name}). {how} "
            f"{settings.safety.kill_hotkey.upper()} stops everything; Ctrl+C quits."
        )
        if not rt.uses_aec:
            log_line("  Echo cancellation is off: use headphones so JARVIS doesn't hear itself.")

    log_line("Loading speech models...")
    try:
        await voice_session(
            settings, paths, provider, log_line, board, _quiet(print_event), on_ready=ready
        )
    except ModelError as exc:
        print(exc, file=sys.stderr)
        return 2
    except asyncio.CancelledError:
        pass
    finally:
        if switch is not None:
            switch.stop()
        await provider.aclose()
    return 0


def _quiet(sink: Callable[[dict[str, Any]], Any]) -> Callable[[dict[str, Any]], Any]:
    """Console feedback for everything except text that is already being spoken."""

    async def wrapped(event: dict[str, Any]) -> None:
        if event.get("type") in {"assistant.text"}:
            return
        await sink(event)

    return wrapped
