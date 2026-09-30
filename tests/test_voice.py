from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from typing import Any

import httpx2 as httpx
import numpy as np
import pytest

from jarvis.core.config import Activation, VoiceConfig
from jarvis.safety.policy import ApprovalRequest, Risk
from jarvis.voice import text as vtext
from jarvis.voice.audio import FRAME, OUT_SR, AudioEvent, EchoCanceller, Frontend, Player
from jarvis.voice.engine import VoiceAssistant
from jarvis.voice.models import FILES, ModelError, ModelFile, ModelStore
from jarvis.voice.speech import tone
from jarvis.voice.vad import WINDOW, Event, Segmenter, SegmenterConfig

# ======================================================================= text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Hey Jarvis, open Notepad.", "open Notepad."),
        ("jarvis what's the time", "what's the time"),
        ("OK Jarvis. Stop.", "Stop."),
        ("Open the Jarvis folder", "Open the Jarvis folder"),
    ],
)
def test_strip_wake_word(raw: str, expected: str) -> None:
    assert vtext.strip_wake_word(raw) == expected


def test_intents() -> None:
    assert vtext.is_stop("Stop!") and vtext.is_stop("never mind.")
    assert not vtext.is_stop("stop the music player and open Spotify")
    assert vtext.is_backchannel("Uh huh.") and not vtext.is_backchannel("open excel")
    assert vtext.parse_yes_no("Yes, go ahead.") is True
    assert vtext.parse_yes_no("No, don't do that") is False
    assert vtext.parse_yes_no("hmm what was the file") is None
    assert vtext.parse_yes_no("yes no") is None


def test_for_speech_cleans_markdown_paths_and_links() -> None:
    out = vtext.for_speech(
        "## Done\n- Saved **C:\\Users\\me\\Documents\\tracker.xlsx**\n"
        "- See https://example.com/x?y=1\n```python\nprint(1)\n```"
    )
    assert "tracker.xlsx" in out and "C:\\" not in out and "**" not in out
    assert "a link" in out and "print(1)" not in out and "#" not in out


def test_sentences_chunking() -> None:
    parts = vtext.sentences("First one. Second one! Then " + "word, " * 80)
    assert parts[0] == "First one." and parts[1] == "Second one!"
    assert all(len(p) <= 220 for p in parts)


# ======================================================================= segmenter


def feed(
    seg: Segmenter, probs: list[float], keep: set[Event] | None = None
) -> list[tuple[Event, Any]]:
    """Push probabilities; by default ignore the speculative PAUSE/RESUME events."""
    keep = keep or {Event.SPEECH_START, Event.UTTERANCE}
    out = []
    for p in probs:
        out += [e for e in seg.push(np.full(WINDOW, p, dtype=np.float32), p) if e[0] in keep]
    return out


def test_segmenter_start_end_and_preroll() -> None:
    seg = Segmenter(SegmenterConfig(start_ms=160, end_silence_ms=320, preroll_ms=320))
    ev = feed(seg, [0.0] * 20 + [0.9] * 4)
    assert ev == []  # 4 windows = 128 ms < 160 ms
    ev = feed(seg, [0.9])
    assert [e for e, _ in ev] == [Event.SPEECH_START]
    ev = feed(seg, [0.9] * 20 + [0.1] * 10)
    assert [e for e, _ in ev] == [Event.UTTERANCE]
    audio = ev[0][1]
    # preroll (10 windows max, incl. the 5 start windows) + 20 speech + 10 silence
    assert audio.shape[0] == (10 + 20 + 10) * WINDOW


def test_segmenter_hysteresis_and_min_length() -> None:
    seg = Segmenter(SegmenterConfig(start_ms=64, end_silence_ms=320, min_utterance_ms=640))
    ev = feed(seg, [0.9, 0.9] + [0.4] * 30 + [0.1] * 10)
    # 0.4 is above neg_threshold (0.35) so it keeps the utterance alive
    assert [e for e, _ in ev] == [Event.SPEECH_START, Event.UTTERANCE]
    seg.reset()
    ev = feed(seg, [0.9, 0.9, 0.1, 0.1] + [0.1] * 10)  # too short -> dropped
    assert [e for e, _ in ev] == [Event.SPEECH_START]


def test_segmenter_pause_and_resume() -> None:
    seg = Segmenter(SegmenterConfig(start_ms=64, early_silence_ms=256, end_silence_ms=640))
    every = set(Event)
    ev = feed(seg, [0.9] * 12 + [0.1] * 8, every)  # 8 x 32 ms = 256 ms of silence
    assert [e for e, _ in ev] == [Event.SPEECH_START, Event.PAUSE]
    ev = feed(seg, [0.9] * 5, every)  # user keeps talking
    assert [e for e, _ in ev] == [Event.RESUME]
    ev = feed(seg, [0.1] * 20, every)
    assert [e for e, _ in ev] == [Event.PAUSE, Event.UTTERANCE]
    pause_audio, final_audio = ev[0][1], ev[1][1]
    # The final utterance is the paused candidate plus trailing silence only.
    assert np.array_equal(final_audio[: pause_audio.shape[0]], pause_audio)


def test_segmenter_max_length() -> None:
    seg = Segmenter(SegmenterConfig(start_ms=32, max_utterance_s=1.0))
    ev = feed(seg, [0.9] * 40)
    # A monologue is cut at the cap; continuing speech starts the next utterance.
    assert [e for e, _ in ev] == [Event.SPEECH_START, Event.UTTERANCE, Event.SPEECH_START]


# ======================================================================= models


def _store_with(tmp_path: Path, body: bytes, sha: str) -> tuple[ModelStore, ModelFile]:
    f = ModelFile("x.onnx", "https://example.test/x.onnx", sha, len(body))
    FILES[f.name] = f
    return ModelStore(tmp_path / "models"), f


def test_model_download_verified(tmp_path: Path) -> None:
    body = b"model-bytes" * 100
    store, f = _store_with(tmp_path, body, hashlib.sha256(body).hexdigest())
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body))
    )
    store.fetch([f.name], client=client)
    assert store.has(f.name) and store.missing([f.name]) == []
    del FILES[f.name]


def test_model_download_rejects_tampered_file(tmp_path: Path) -> None:
    body = b"evil" * 10
    store, f = _store_with(tmp_path, body, "0" * 64)
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body))
    )
    with pytest.raises(ModelError, match="verification"):
        store.fetch([f.name], client=client)
    assert not store.path(f.name).exists() and not store.path(f.name + ".part").exists()
    del FILES[f.name]


def test_pinned_model_urls() -> None:
    for f in FILES.values():
        assert f.url.startswith("https://") and len(f.sha256) == 64
        assert "/main/" not in f.url  # always a pinned release or commit


# ======================================================================= audio pieces


def test_player_pause_resume_clear_and_idle() -> None:
    idle: list[int] = []
    p = Player(on_idle=lambda: idle.append(1))
    p.enqueue(np.ones(300, dtype=np.float32))
    assert p.active and p.playing
    assert p.fill(220).sum() == 220
    p.pause()
    assert p.fill(220).sum() == 0 and p.active and not p.playing
    p.resume()
    assert p.fill(220).sum() == 80
    p.fill(220)
    assert idle == [1] and not p.active
    p.enqueue(np.ones(1000, dtype=np.float32))
    p.clear()
    assert not p.active and p.fill(220).sum() == 0


def test_player_produces_aec_reference_frames() -> None:
    p = Player()
    p.enqueue(np.full(OUT_SR, 0.5, dtype=np.float32))
    for _ in range(20):
        p.fill(220)
    frames = []
    while not p.reference.empty():
        frames.append(p.reference.get_nowait())
    assert frames and all(f.shape == (FRAME,) and f.dtype == np.int16 for f in frames)


def test_echo_canceller_removes_echo() -> None:
    aec = EchoCanceller(True, delay_ms=0)
    if not aec.active:
        pytest.skip("echo canceller not available")
    t = np.arange(FRAME * 300) / 16000
    far = (np.sin(2 * np.pi * 440 * t) * 8000).astype(np.int16)
    near = (far * 0.5).astype(np.int16)
    levels = []
    for i in range(0, far.shape[0], FRAME):
        aec.render(far[i : i + FRAME])
        levels.append(np.abs(aec.capture(near[i : i + FRAME])).mean())
    assert np.mean(levels[-100:]) < 0.05 * np.abs(near).mean()


class _FakeVAD:
    def __init__(self, probs: list[float]) -> None:
        self.probs = probs

    def __call__(self, w: np.ndarray) -> float:
        return self.probs.pop(0) if self.probs else 0.0


class _FakeWake:
    def __init__(self) -> None:
        self.calls = 0

    def process(self, pcm: np.ndarray) -> bool:
        self.calls += 1
        return self.calls == 3

    def reset(self) -> None:
        pass


def test_frontend_posts_events() -> None:
    posted: list[AudioEvent] = []
    fe = Frontend(
        posted.append,
        EchoCanceller(False),
        Segmenter(SegmenterConfig(start_ms=32, end_silence_ms=64, min_utterance_ms=32)),
        _FakeVAD([0.9, 0.9, 0.1, 0.1, 0.1]),  # type: ignore[arg-type]
        _FakeWake(),
        Player(),
        clock=lambda: 1.0,
    )
    for _ in range(16):  # 16 x 10 ms = 5 VAD windows
        fe.process(np.zeros(FRAME, dtype=np.int16))
    assert [e.kind for e in posted if e.kind != "pause"] == ["wake", "speech_start", "utterance"]


def test_without_aec_speech_while_playing_is_ignored() -> None:
    posted: list[AudioEvent] = []
    player = Player()
    player.enqueue(np.ones(OUT_SR, dtype=np.float32))
    fe = Frontend(
        posted.append,
        EchoCanceller(False),
        Segmenter(SegmenterConfig(start_ms=32)),
        _FakeVAD([0.8] * 10),  # type: ignore[arg-type]
        None,
        player,
        clock=lambda: 1.0,
    )
    for _ in range(32):
        fe.process(np.zeros(FRAME, dtype=np.int16))
    assert posted == []


def test_tone_is_clean() -> None:
    t = tone(ms=100)
    assert t.dtype == np.float32 and abs(t[0]) < 1e-3 and abs(t[-1]) < 1e-2


# ======================================================================= engine


class FakePlayer:
    def __init__(self) -> None:
        self.queued: list[np.ndarray] = []
        self.paused = False
        self.events: list[str] = []

    def enqueue(self, audio: np.ndarray) -> None:
        self.queued.append(audio)

    def pause(self) -> None:
        self.paused = True
        self.events.append("pause")

    def resume(self) -> None:
        self.paused = False
        self.events.append("resume")

    def clear(self) -> None:
        self.queued.clear()
        self.paused = False
        self.events.append("clear")

    @property
    def active(self) -> bool:
        return bool(self.queued)

    @property
    def playing(self) -> bool:
        return bool(self.queued) and not self.paused


class FakeSTT:
    def __init__(self) -> None:
        self.next: list[str] = []
        self.calls = 0

    async def transcribe(self, audio: np.ndarray) -> str:
        self.calls += 1
        return self.next.pop(0)


class FakeTTS:
    def __init__(self) -> None:
        self.spoken: list[str] = []

    async def synthesize(self, text: str) -> np.ndarray:
        self.spoken.append(text)
        return np.ones(10, dtype=np.float32)


class FakeAgent:
    def __init__(self, va: VoiceAssistant, block: bool = False, reply: str = "Done.") -> None:
        self.va = va
        self.goals: list[str] = []
        self.block = block
        self.reply = reply
        self.cancelled = 0

    async def run(self, goal: str, voice: bool = False) -> None:
        assert voice
        self.goals.append(goal)
        try:
            if self.block:
                await asyncio.sleep(3600)
            await self.va.on_agent_event({"type": "assistant.text", "text": self.reply})
            await self.va.on_agent_event(
                {"type": "task.finished", "status": "completed", "summary": self.reply}
            )
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


AUDIO = np.zeros(1600, dtype=np.float32)


async def make_va(
    activation: Activation = Activation.WAKE_WORD, **cfg: Any
) -> tuple[VoiceAssistant, FakePlayer, FakeSTT, FakeTTS, Clock, asyncio.Task[None]]:
    player, stt, tts, clock = FakePlayer(), FakeSTT(), FakeTTS(), Clock()
    va = VoiceAssistant(
        VoiceConfig(activation=activation, **cfg),
        stt,
        tts,
        player,
        clock=clock,
        log_line=lambda s: None,
    )
    runner = asyncio.create_task(va.run())
    await asyncio.sleep(0)
    return va, player, stt, tts, clock, runner


async def settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


async def utter(va: VoiceAssistant, stt: FakeSTT, text: str, clock: Clock) -> None:
    stt.next.append(text)
    await va.handle(AudioEvent("speech_start", at=clock()))
    await va.handle(AudioEvent("utterance", AUDIO, at=clock() + 1))
    await settle()


async def test_idle_speech_without_wake_word_is_ignored() -> None:
    va, _, stt, _, clock, runner = await make_va()
    agent = FakeAgent(va)
    va.attach(agent)
    await utter(va, stt, "open notepad", clock)
    assert agent.goals == [] and stt.calls == 0  # not even transcribed
    runner.cancel()


async def test_wake_word_then_request() -> None:
    va, _, stt, tts, clock, runner = await make_va()
    agent = FakeAgent(va, reply="Opening Notepad.")
    va.attach(agent)
    await va.handle(AudioEvent("wake", at=clock()))
    await utter(va, stt, "Hey Jarvis, open notepad.", clock)
    assert agent.goals == ["open notepad."]
    assert tts.spoken == ["Opening Notepad."]
    runner.cancel()


async def test_follow_up_window() -> None:
    va, player, stt, _, clock, runner = await make_va(follow_up_s=5)
    agent = FakeAgent(va)
    va.attach(agent)
    await va.handle(AudioEvent("wake", at=clock()))
    await utter(va, stt, "first", clock)
    player.queued.clear()  # the reply finished playing
    clock.t += 20  # wake window over
    va._on_idle()  # JARVIS finished speaking -> follow-up window opens
    await utter(va, stt, "second", clock)
    assert agent.goals == ["first", "second"]
    player.queued.clear()
    clock.t += 10  # follow-up window closed
    await utter(va, stt, "third", clock)
    assert agent.goals == ["first", "second"]
    runner.cancel()


async def test_barge_in_backchannel_resumes() -> None:
    va, player, stt, _, clock, runner = await make_va(Activation.ALWAYS)
    va.attach(FakeAgent(va))
    player.enqueue(np.ones(10, dtype=np.float32))  # JARVIS is talking
    stt.next.append("uh huh")
    await va.handle(AudioEvent("speech_start", at=clock()))
    assert player.paused  # paused immediately, before any transcription
    await va.handle(AudioEvent("utterance", AUDIO, at=clock()))
    assert player.events == ["pause", "resume"] and player.active
    runner.cancel()


async def test_barge_in_new_instruction_changes_course() -> None:
    va, player, stt, _, clock, runner = await make_va(Activation.ALWAYS)
    agent = FakeAgent(va, block=True)
    va.attach(agent)
    await utter(va, stt, "write a long report", clock)
    assert va.task_running
    player.enqueue(np.ones(10, dtype=np.float32))
    await utter(va, stt, "actually make it a spreadsheet", clock)
    assert agent.cancelled == 1
    assert agent.goals == ["write a long report", "actually make it a spreadsheet"]
    assert "clear" in player.events and va.task_running
    runner.cancel()


async def test_stop_phrase_cancels_and_confirms() -> None:
    va, _, stt, tts, clock, runner = await make_va(Activation.ALWAYS)
    agent = FakeAgent(va, block=True)
    va.attach(agent)
    await utter(va, stt, "organise my downloads", clock)
    await utter(va, stt, "Stop.", clock)
    assert agent.cancelled == 1 and not va.task_running
    assert tts.spoken == ["Okay, stopped."]
    runner.cancel()


async def test_spoken_approval_yes_and_no() -> None:
    va, _, stt, tts, clock, runner = await make_va(Activation.ALWAYS)
    req = ApprovalRequest("delete_path", "Move old.txt to the Recycle Bin", Risk.DESTRUCTIVE)

    decision_task = asyncio.create_task(va.approver.request(req))
    await settle()
    assert "Should I go ahead?" in tts.spoken[-1]
    await utter(va, stt, "yes please", clock)
    assert (await decision_task).approved

    decision_task = asyncio.create_task(va.approver.request(req))
    await settle()
    await utter(va, stt, "hmm", clock)  # ambiguous -> asks again
    await settle()
    assert tts.spoken[-1] == "Sorry, was that a yes or a no?"
    await utter(va, stt, "no", clock)
    assert not (await decision_task).approved
    runner.cancel()


async def test_approval_timeout_declines() -> None:
    va, _, _, _, _, runner = await make_va(Activation.ALWAYS, approval_timeout_s=5)
    va.cfg.approval_timeout_s = 0.05  # bypass validation for a fast test
    decision = await va.approver.request(
        ApprovalRequest("run_shell", "Run a command", Risk.EXECUTE)
    )
    assert not decision.approved
    runner.cancel()


async def test_speculative_transcript_is_reused() -> None:
    va, _, stt, _, clock, runner = await make_va(Activation.ALWAYS)
    agent = FakeAgent(va)
    va.attach(agent)
    stt.next.append("open notepad")
    await va.handle(AudioEvent("speech_start", at=clock()))
    await va.handle(AudioEvent("pause", AUDIO, at=clock()))
    await va.handle(AudioEvent("utterance", AUDIO, at=clock()))
    await settle()
    assert stt.calls == 1 and agent.goals == ["open notepad"]
    runner.cancel()


async def test_speculative_transcript_discarded_on_resume() -> None:
    va, _, stt, _, clock, runner = await make_va(Activation.ALWAYS)
    agent = FakeAgent(va)
    va.attach(agent)
    stt.next += ["open notepad please"]
    await va.handle(AudioEvent("speech_start", at=clock()))
    await va.handle(AudioEvent("pause", AUDIO, at=clock()))
    await va.handle(AudioEvent("resume", at=clock()))
    await va.handle(AudioEvent("utterance", AUDIO, at=clock()))
    await settle()
    assert agent.goals == ["open notepad please"]
    assert stt.calls == 1  # the stale speculative job never ran
    runner.cancel()


async def test_failures_are_spoken() -> None:
    va, _, _, tts, _, runner = await make_va()
    await va.on_agent_event({"type": "task.finished", "status": "failed", "summary": "No API key."})
    await settle()
    assert tts.spoken == ["No API key."]
    await va.on_agent_event({"type": "task.finished", "status": "cancelled", "summary": "x"})
    await settle()
    assert tts.spoken == ["No API key."]
    runner.cancel()


async def test_dropped_speech_is_not_synthesized() -> None:
    va, player, _, tts, _, runner = await make_va()
    await va.say("One. Two. Three.")
    va._drop_speech()
    await settle()
    assert tts.spoken == [] and player.queued == []
    runner.cancel()


# ======================================================================= real models (opt-in)

MODELS = os.environ.get("JARVIS_TEST_MODELS")


@pytest.mark.skipif(not MODELS, reason="set JARVIS_TEST_MODELS to a folder with voice models")
def test_wake_word_on_synthesized_speech() -> None:
    import soxr

    from jarvis.voice.speech import PiperTTS
    from jarvis.voice.wake import WakeWordDetector

    m = Path(MODELS or "")
    tts = PiperTTS(m / "en_US-amy-medium.onnx")
    det = WakeWordDetector(
        m / "melspectrogram.onnx", m / "embedding_model.onnx", m / "hey_jarvis_v0.1.onnx"
    )

    def score(text: str) -> float:
        audio = asyncio.run(tts.synthesize(text))
        pcm = (soxr.resample(audio, OUT_SR, 16000) * 26000).astype(np.int16)
        pcm = np.concatenate([np.zeros(16000, np.int16), pcm, np.zeros(16000, np.int16)])
        det.reset()
        best = 0.0
        for i in range(0, pcm.shape[0], FRAME):
            det.process(pcm[i : i + FRAME])
            best = max(best, det.last_score)
        return best

    assert score("Hey Jarvis.") > 0.5
    assert score("Please open the file for me.") < 0.2


async def test_busy_when_another_client_holds_the_lock() -> None:
    lock = asyncio.Lock()
    player, stt, tts, clock = FakePlayer(), FakeSTT(), FakeTTS(), Clock()
    va = VoiceAssistant(
        VoiceConfig(activation=Activation.ALWAYS), stt, tts, player,
        clock=clock, log_line=lambda s: None, task_lock=lock,
    )  # fmt: skip
    runner = asyncio.create_task(va.run())
    agent = FakeAgent(va)
    va.attach(agent)
    async with lock:  # e.g. the desktop UI is running a task
        await utter(va, stt, "open excel", clock)
    assert agent.goals == [] and tts.spoken == ["I'm busy with another task right now."]
    runner.cancel()


async def test_kill_switch_silences_voice() -> None:
    from jarvis.server.session import TaskBoard

    va, player, stt, _, clock, runner = await make_va(Activation.ALWAYS)
    board = TaskBoard()
    board.on_kill(va.silence)
    va._track = board.track
    agent = FakeAgent(va, block=True)
    va.attach(agent)
    await utter(va, stt, "long job", clock)
    player.enqueue(np.ones(10, dtype=np.float32))
    assert board.cancel_all() == 1
    await settle()
    assert agent.cancelled == 1 and not player.active
    runner.cancel()
