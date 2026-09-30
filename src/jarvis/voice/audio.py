"""Real-time audio: microphone capture, speaker playback, echo cancellation.

Threads:
- PortAudio input callback: pushes 10 ms mic frames onto a queue.
- PortAudio output callback: plays queued speech and records exactly what was played
  (resampled to 16 kHz) as the echo canceller's reference signal.
- Front-end thread: echo-cancels each mic frame, then runs wake word + VAD and posts
  events to the asyncio loop.

Nothing here blocks the event loop.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

from jarvis.voice.vad import WINDOW, Event, Segmenter, SileroVAD

log = logging.getLogger(__name__)

MIC_SR = 16000
FRAME = 160  # 10 ms at 16 kHz - the echo canceller's frame size
OUT_SR = 22050  # Piper and SAPI both produce 22.05 kHz
OUT_BLOCK = 220  # 10 ms at 22.05 kHz


# ----------------------------------------------------------------------------- AEC


class EchoCanceller:
    """WebRTC audio processing (echo cancellation, noise suppression, high-pass)."""

    def __init__(self, enabled: bool, delay_ms: int = 80) -> None:
        self._apm: Any = None
        if not enabled:
            return
        try:
            from livekit import rtc

            self._rtc = rtc
            self._apm = rtc.AudioProcessingModule(
                echo_cancellation=True,
                noise_suppression=True,
                high_pass_filter=True,
                auto_gain_control=True,
            )
            self._apm.set_stream_delay_ms(delay_ms)
        except Exception:  # missing native library -> run without AEC
            log.warning("echo cancellation unavailable; barge-in needs headphones", exc_info=True)
            self._apm = None

    @property
    def active(self) -> bool:
        return self._apm is not None

    def set_delay(self, ms: int) -> None:
        if self._apm is not None:
            self._apm.set_stream_delay_ms(max(0, min(500, ms)))

    def render(self, ref: np.ndarray) -> None:
        if self._apm is not None:
            frame = self._rtc.AudioFrame(ref.astype(np.int16).tobytes(), MIC_SR, 1, FRAME)
            self._apm.process_reverse_stream(frame)

    def capture(self, mic: np.ndarray) -> np.ndarray:
        if self._apm is None:
            return mic
        frame = self._rtc.AudioFrame(mic.astype(np.int16).tobytes(), MIC_SR, 1, FRAME)
        self._apm.process_stream(frame)
        return np.frombuffer(bytes(frame.data), dtype=np.int16).copy()


# ----------------------------------------------------------------------------- playback


class Player:
    """Speech output with instant pause/resume/clear for barge-in."""

    def __init__(self, on_idle: Callable[[], None] | None = None) -> None:
        self._chunks: deque[np.ndarray] = deque()
        self._pos = 0
        self._lock = threading.Lock()
        self._paused = False
        self._was_active = False
        self._on_idle = on_idle
        self.reference: queue.Queue[np.ndarray] = queue.Queue(maxsize=500)
        self._resampler: Any = None
        self._ref_buf = np.zeros(0, dtype=np.float32)

    # -- control (any thread) --

    def enqueue(self, audio: np.ndarray) -> None:
        with self._lock:
            self._chunks.append(audio.astype(np.float32))

    def pause(self) -> None:
        with self._lock:
            self._paused = True

    def resume(self) -> None:
        with self._lock:
            self._paused = False

    def clear(self) -> None:
        with self._lock:
            self._chunks.clear()
            self._pos = 0
            self._paused = False

    @property
    def active(self) -> bool:
        """Has speech queued (playing or paused)."""
        with self._lock:
            return bool(self._chunks)

    @property
    def playing(self) -> bool:
        with self._lock:
            return bool(self._chunks) and not self._paused

    # -- audio thread --

    def fill(self, n: int) -> np.ndarray:
        out = np.zeros(n, dtype=np.float32)
        idle_now = False
        with self._lock:
            if not self._paused:
                i = 0
                while i < n and self._chunks:
                    cur = self._chunks[0]
                    take = min(n - i, cur.shape[0] - self._pos)
                    out[i : i + take] = cur[self._pos : self._pos + take]
                    i += take
                    self._pos += take
                    if self._pos >= cur.shape[0]:
                        self._chunks.popleft()
                        self._pos = 0
            active = bool(self._chunks)
            if self._was_active and not active:
                idle_now = True
            self._was_active = active
        self._record_reference(out)
        if idle_now and self._on_idle:
            self._on_idle()
        return out

    def _record_reference(self, played: np.ndarray) -> None:
        """What the speaker is emitting, as 16 kHz int16 frames for the echo canceller."""
        if self._resampler is None:
            import soxr

            self._resampler = soxr.ResampleStream(OUT_SR, MIC_SR, 1, dtype="float32")
        res = self._resampler.resample_chunk(played)
        self._ref_buf = np.concatenate([self._ref_buf, res])
        while self._ref_buf.shape[0] >= FRAME:
            frame, self._ref_buf = self._ref_buf[:FRAME], self._ref_buf[FRAME:]
            pcm = (np.clip(frame, -1, 1) * 32767).astype(np.int16)
            with contextlib.suppress(queue.Full):
                self.reference.put_nowait(pcm)


# ----------------------------------------------------------------------------- front-end


_KIND = {
    Event.SPEECH_START: "speech_start",
    Event.PAUSE: "pause",
    Event.RESUME: "resume",
    Event.UTTERANCE: "utterance",
}


@dataclass(frozen=True)
class AudioEvent:
    kind: str  # "wake" | "speech_start" | "pause" | "resume" | "utterance"
    audio: np.ndarray | None = None
    at: float = 0.0


class Frontend:
    """Echo cancellation -> wake word -> VAD -> segmentation, on its own thread."""

    def __init__(
        self,
        post: Callable[[AudioEvent], None],
        aec: EchoCanceller,
        segmenter: Segmenter,
        vad: SileroVAD,
        wake: Any | None,
        player: Player,
        clock: Callable[[], float],
    ) -> None:
        self._post = post
        self._aec = aec
        self._seg = segmenter
        self._vad = vad
        self._wake = wake
        self._player = player
        self._clock = clock
        self.mic: queue.Queue[np.ndarray] = queue.Queue(maxsize=1000)
        self._win = np.zeros(0, dtype=np.float32)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.level = 0.0  # recent mic level, for diagnostics

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="voice-frontend", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                frame = self.mic.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self.process(frame)
            except Exception:
                log.exception("voice front-end error")

    def process(self, frame: np.ndarray) -> None:
        # Feed everything the speaker played since the last mic frame.
        while True:
            try:
                self._aec.render(self._player.reference.get_nowait())
            except queue.Empty:
                break
        clean = self._aec.capture(frame)
        self.level = 0.9 * self.level + 0.1 * float(np.abs(clean).mean())
        if self._wake is not None and self._wake.process(clean):
            self._wake.reset()
            self._post(AudioEvent("wake", at=self._clock()))
        self._win = np.concatenate([self._win, clean.astype(np.float32) / 32768.0])
        while self._win.shape[0] >= WINDOW:
            w, self._win = self._win[:WINDOW], self._win[WINDOW:]
            prob = self._vad(w)
            if not self._aec.active and self._player.playing and prob < 0.9:
                # Without echo cancellation, JARVIS would hear itself; only very
                # confident speech counts while it's talking (use headphones).
                prob = 0.0
            for ev, audio in self._seg.push(w, prob):
                self._post(AudioEvent(_KIND[ev], audio, at=self._clock()))


class AudioDevices:
    """Opens the PortAudio streams and wires them to the player and front-end."""

    def __init__(
        self,
        frontend: Frontend,
        player: Player,
        aec: EchoCanceller,
        input_device: int | str | None = None,
        output_device: int | str | None = None,
    ) -> None:
        self._fe = frontend
        self._player = player
        self._aec = aec
        self._in_dev = input_device
        self._out_dev = output_device
        self._streams: list[Any] = []

    def start(self) -> None:
        import sounddevice as sd

        def on_input(indata: Any, frames: int, time: Any, status: Any) -> None:
            pcm = np.frombuffer(bytes(indata), dtype=np.int16)
            for i in range(0, pcm.shape[0] - FRAME + 1, FRAME):
                with contextlib.suppress(queue.Full):
                    self._fe.mic.put_nowait(pcm[i : i + FRAME].copy())

        def on_output(outdata: Any, frames: int, time: Any, status: Any) -> None:
            outdata[:, 0] = self._player.fill(frames)

        inp = sd.RawInputStream(
            samplerate=MIC_SR,
            channels=1,
            dtype="int16",
            blocksize=FRAME,
            device=self._in_dev,
            callback=on_input,
        )
        out = sd.OutputStream(
            samplerate=OUT_SR,
            channels=1,
            dtype="float32",
            blocksize=OUT_BLOCK,
            device=self._out_dev,
            callback=on_output,
        )
        out.start()
        inp.start()
        self._streams = [inp, out]
        # Echo path delay ~ output latency + input latency.
        self._aec.set_delay(int((inp.latency + out.latency) * 1000))
        self._fe.start()

    def stop(self) -> None:
        self._fe.stop()
        for s in self._streams:
            try:
                s.stop()
                s.close()
            except Exception:
                log.debug("error closing audio stream", exc_info=True)
        self._streams = []
