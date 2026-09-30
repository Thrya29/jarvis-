"""Full-duplex conversation engine.

The microphone never stops. What JARVIS does with speech depends on the moment:

- Idle: only an utterance containing the wake word ("Hey Jarvis ...") is accepted.
- In conversation (JARVIS is working or speaking, a question is pending, or it just
  finished speaking): any utterance is accepted, no wake word needed.
- Barge-in: when the user starts talking while JARVIS speaks, playback pauses within
  one VAD step (~160 ms). The utterance is then transcribed:
    * a backchannel ("okay", "uh-huh") or nothing intelligible -> resume speaking;
    * "stop" / "cancel" / "never mind" -> cancel the task, stop speaking;
    * anything else -> drop the rest of the speech, cancel the running task, and start
      a new turn with what the user said (same conversation, so JARVIS adapts).
- Approvals and clarifying questions are asked aloud and answered by voice.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import numpy as np

from jarvis.agent.events import Event as AgentEvent
from jarvis.core.config import Activation, VoiceConfig
from jarvis.safety.policy import ApprovalDecision, ApprovalRequest
from jarvis.voice.audio import AudioEvent
from jarvis.voice.text import (
    for_speech,
    is_backchannel,
    is_stop,
    parse_yes_no,
    sentences,
    strip_wake_word,
)

log = logging.getLogger(__name__)

WAKE_WINDOW_S = 8.0  # after "Hey Jarvis" alone, how long to wait for the request
WAKE_LOOKBACK_S = 2.0  # a wake word this soon before an utterance started still counts


class PlayerLike(Protocol):
    def enqueue(self, audio: np.ndarray) -> None: ...
    def pause(self) -> None: ...
    def resume(self) -> None: ...
    def clear(self) -> None: ...
    @property
    def active(self) -> bool: ...
    @property
    def playing(self) -> bool: ...


class STTLike(Protocol):
    async def transcribe(self, audio: np.ndarray) -> str: ...


class TTSLike(Protocol):
    async def synthesize(self, text: str) -> np.ndarray: ...


class AgentLike(Protocol):
    async def run(self, goal: str, voice: bool = False) -> Any: ...


class VoiceApprover:
    """Asks for approval out loud and listens for yes/no."""

    def __init__(self, assistant: VoiceAssistant) -> None:
        self._va = assistant

    async def request(self, req: ApprovalRequest) -> ApprovalDecision:
        answer = await self._va.ask(f"{for_speech(req.summary)} Should I go ahead?")
        decision = parse_yes_no(answer)
        if decision is None and answer:
            answer = await self._va.ask("Sorry, was that a yes or a no?")
            decision = parse_yes_no(answer)
        if decision is None:
            self._va.log("  (no clear answer - declined)")
            return ApprovalDecision(False, "no clear spoken answer")
        return ApprovalDecision(decision, "" if decision else answer)

    async def ask(self, question: str) -> str:
        return await self._va.ask(question)


class VoiceAssistant:
    def __init__(
        self,
        cfg: VoiceConfig,
        stt: STTLike,
        tts: TTSLike,
        player: PlayerLike,
        chime: np.ndarray | None = None,
        clock: Callable[[], float] = time.monotonic,
        log_line: Callable[[str], None] = print,
        task_lock: asyncio.Lock | None = None,
        track: Callable[[asyncio.Task[Any]], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self._stt = stt
        self._tts = tts
        self._player = player
        self._chime = chime
        self._clock = clock
        self.log = log_line
        # In the daemon, voice shares the one-task-at-a-time lock with UI clients.
        self._task_lock = task_lock
        self._track = track
        self.approver = VoiceApprover(self)
        self.events: asyncio.Queue[AudioEvent] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._agent: AgentLike | None = None
        self._task: asyncio.Task[Any] | None = None
        self._answer: asyncio.Future[str] | None = None
        self._speech: asyncio.Queue[tuple[int, str]] = asyncio.Queue()
        self._gen = 0  # bumped whenever pending speech is discarded
        self._pending_speech = 0
        self._wake_at = -math.inf
        self._speech_start_at = -math.inf
        self._accept_current = False
        self._follow_until = -math.inf
        self._paused_for_barge = False
        self._spoke_this_task = False
        self._early: asyncio.Task[str] | None = None  # speculative transcription
        self._closed = asyncio.Event()

    # ------------------------------------------------------------------ wiring

    def attach(self, agent: AgentLike) -> None:
        self._agent = agent

    def post(self, event: AudioEvent) -> None:
        """Thread-safe entry point for the audio front-end."""
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self.events.put_nowait, event)

    def player_idle(self) -> None:
        """Called from the audio thread when speech playback finishes."""
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._on_idle)

    def _on_idle(self) -> None:
        self._follow_until = self._clock() + self.cfg.follow_up_s

    @property
    def task_running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def conversation_active(self) -> bool:
        now = self._clock()
        return (
            self.cfg.activation is Activation.ALWAYS
            or self.task_running
            or self._player.active
            or self._pending_speech > 0
            or self._answer is not None
            or now < self._follow_until
            or now - self._wake_at < WAKE_WINDOW_S
        )

    # ------------------------------------------------------------------ main loop

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        speaker = asyncio.create_task(self._speech_worker())
        try:
            while not self._closed.is_set():
                getter = asyncio.create_task(self.events.get())
                closer = asyncio.create_task(self._closed.wait())
                done, _ = await asyncio.wait({getter, closer}, return_when=asyncio.FIRST_COMPLETED)
                closer.cancel()
                if getter not in done:
                    getter.cancel()
                    break
                try:
                    await self.handle(getter.result())
                except Exception:
                    log.exception("voice event handling failed")
        finally:
            speaker.cancel()
            await self.stop_all()

    def close(self) -> None:
        self._closed.set()

    async def handle(self, ev: AudioEvent) -> None:
        if ev.kind == "wake":
            self._on_wake(ev)
        elif ev.kind == "speech_start":
            self._on_speech_start(ev)
        elif ev.kind == "pause" and ev.audio is not None:
            if self._accepted(ev):
                # Start transcribing during the trailing silence; reused if the user
                # really has finished, discarded if they keep talking.
                self._early = asyncio.create_task(self._stt.transcribe(ev.audio))
        elif ev.kind == "resume":
            # The user kept talking: that candidate transcript is stale.
            if self._early is not None:
                self._early.cancel()
            self._early = None
        elif ev.kind == "utterance" and ev.audio is not None:
            await self._on_utterance(ev)

    def _on_wake(self, ev: AudioEvent) -> None:
        self._wake_at = ev.at
        self.log("  (listening)")
        if self._player.playing:
            self._player.pause()
            self._paused_for_barge = True
        elif self._chime is not None:
            self._player.enqueue(self._chime)

    def _on_speech_start(self, ev: AudioEvent) -> None:
        self._speech_start_at = ev.at
        self._accept_current = self.conversation_active
        if self._accept_current and self.cfg.barge_in and self._player.playing:
            self._player.pause()
            self._paused_for_barge = True

    def _accepted(self, ev: AudioEvent) -> bool:
        woke = self._wake_at >= self._speech_start_at - WAKE_LOOKBACK_S
        return self._accept_current or woke

    async def _on_utterance(self, ev: AudioEvent) -> None:
        early, self._early = self._early, None
        woke = self._wake_at >= self._speech_start_at - WAKE_LOOKBACK_S
        if not (self._accept_current or woke):
            self._resume_if_paused()
            return
        assert ev.audio is not None
        raw = await early if early is not None else await self._stt.transcribe(ev.audio)
        text = (
            strip_wake_word(raw)
            if woke or raw.lower().lstrip().startswith(("hey", "jarvis"))
            else raw
        )
        if text:
            self.log(f"\nyou (voice)> {text}")
        await self.handle_text(text)

    def _resume_if_paused(self) -> None:
        if self._paused_for_barge:
            self._paused_for_barge = False
            self._player.resume()

    def _drop_speech(self) -> None:
        self._gen += 1
        self._player.clear()
        self._paused_for_barge = False

    async def handle_text(self, text: str) -> None:
        if self._answer is not None and not self._answer.done():
            self._drop_speech()
            self._answer.set_result(text)
            return
        if not text or (is_backchannel(text) and (self.task_running or self._player.active)):
            self._resume_if_paused()
            return
        self._drop_speech()
        if is_stop(text):
            if self.task_running:
                await self._cancel_task()
                await self.say("Okay, stopped.")
            return
        if self.task_running:
            self.log("  (changing course)")
            await self._cancel_task()
        if self._agent is None:
            await self.say("I'm not ready yet.")
            return
        if self._task_lock is not None and self._task_lock.locked():
            await self.say("I'm busy with another task right now.")
            return
        self._task = asyncio.create_task(self._run_goal(text))
        if self._track is not None:
            self._track(self._task)

    async def _run_goal(self, text: str) -> None:
        assert self._agent is not None
        self._spoke_this_task = False
        if self._task_lock is None:
            await self._agent.run(text, voice=True)
        else:
            async with self._task_lock:
                await self._agent.run(text, voice=True)

    async def _cancel_task(self) -> None:
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._answer is not None and not self._answer.done():
            self._answer.set_result("")

    def silence(self) -> None:
        """Thread-safe: stop speaking now (kill switch)."""
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._drop_speech)
        else:
            self._drop_speech()

    async def stop_all(self) -> None:
        """Kill switch / shutdown: stop the task and go quiet immediately."""
        self._drop_speech()
        await self._cancel_task()

    # ------------------------------------------------------------------ agent events

    async def on_agent_event(self, event: AgentEvent) -> None:
        kind = event.get("type")
        if kind == "assistant.text":
            self._spoke_this_task = True
            await self.say(for_speech(str(event["text"])))
        elif kind == "task.finished":
            status = event.get("status")
            if status in {"failed", "refused", "limit_reached"} or (
                status == "completed" and not self._spoke_this_task
            ):
                await self.say(for_speech(str(event.get("summary", ""))))

    # ------------------------------------------------------------------ speaking

    async def say(self, text: str) -> None:
        for s in sentences(text):
            self._pending_speech += 1
            await self._speech.put((self._gen, s))

    async def _speech_worker(self) -> None:
        while True:
            gen, sentence = await self._speech.get()
            try:
                if gen != self._gen:
                    continue
                audio = await self._tts.synthesize(sentence)
                if gen == self._gen and audio.size:
                    self.log(f"jarvis> {sentence}")
                    self._player.enqueue(audio)
            except Exception:
                log.exception("speech synthesis failed")
            finally:
                self._pending_speech -= 1

    async def ask(self, question: str) -> str:
        """Speak a question and wait for the spoken answer ("" on timeout)."""
        loop = asyncio.get_running_loop()
        self._answer = loop.create_future()
        try:
            await self.say(question)
            return await asyncio.wait_for(self._answer, self.cfg.approval_timeout_s)
        except TimeoutError:
            return ""
        finally:
            self._answer = None


def combine_sinks(
    *sinks: Callable[[AgentEvent], Awaitable[None]],
) -> Callable[[AgentEvent], Awaitable[None]]:
    async def sink(event: AgentEvent) -> None:
        for s in sinks:
            await s(event)

    return sink
