"""Voice activity detection (Silero VAD v6, ONNX) and utterance segmentation."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path

import numpy as np

SR = 16000
WINDOW = 512  # 32 ms: Silero's native step at 16 kHz
CONTEXT = 64


def silero_model_path() -> Path:
    """faster-whisper ships the Silero VAD model, so no extra download is needed."""
    import faster_whisper

    return Path(faster_whisper.__file__).parent / "assets" / "silero_vad_v6.onnx"


class SileroVAD:
    def __init__(self, path: Path | None = None) -> None:
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level = 3
        self._s = ort.InferenceSession(
            str(path or silero_model_path()), sess_options=opts, providers=["CPUExecutionProvider"]
        )
        self.reset()

    def reset(self) -> None:
        self._h = np.zeros((1, 1, 128), dtype=np.float32)
        self._c = np.zeros((1, 1, 128), dtype=np.float32)
        self._ctx = np.zeros(CONTEXT, dtype=np.float32)

    def __call__(self, window: np.ndarray) -> float:
        """Speech probability for one 512-sample float32 window in [-1, 1]."""
        x = np.concatenate([self._ctx, window])[None, :].astype(np.float32)
        out, self._h, self._c = self._s.run(None, {"input": x, "h": self._h, "c": self._c})
        self._ctx = window[-CONTEXT:]
        return float(np.asarray(out).reshape(-1)[0])


class Event(Enum):
    SPEECH_START = auto()  # confirmed start of speech (drives barge-in)
    PAUSE = auto()  # short silence: the utterance may be over (speculative transcription)
    RESUME = auto()  # speech continued after a PAUSE: that candidate is stale
    UTTERANCE = auto()  # a complete utterance ended by silence


@dataclass
class SegmenterConfig:
    threshold: float = 0.5
    # Hysteresis: once speaking, lower probabilities still count as speech.
    neg_threshold: float = 0.35
    start_ms: int = 160  # speech needed before SPEECH_START (keeps clicks/coughs out)
    end_silence_ms: int = 700  # silence that ends an utterance
    early_silence_ms: int = 250  # silence that triggers a speculative PAUSE
    preroll_ms: int = 320  # audio kept from before the confirmed start
    max_utterance_s: float = 30.0
    min_utterance_ms: int = 300  # shorter segments are dropped as noise


@dataclass
class Segmenter:
    """Turns per-window speech probabilities into speech-start and utterance events.

    Pure logic (no model), so it's unit-testable with synthetic probabilities.
    """

    cfg: SegmenterConfig = field(default_factory=SegmenterConfig)

    def __post_init__(self) -> None:
        win_ms = WINDOW * 1000 // SR
        self._start_windows = max(1, self.cfg.start_ms // win_ms)
        self._end_windows = max(1, self.cfg.end_silence_ms // win_ms)
        self._early_windows = min(
            self._end_windows - 1, max(1, self.cfg.early_silence_ms // win_ms)
        )
        self._max_windows = int(self.cfg.max_utterance_s * 1000 // win_ms)
        self._min_windows = max(1, self.cfg.min_utterance_ms // win_ms)
        self._pre: deque[np.ndarray] = deque(maxlen=max(1, self.cfg.preroll_ms // win_ms))
        self.reset()

    def reset(self) -> None:
        self._in_speech = False
        self._voiced_run = 0
        self._silence_run = 0
        self._buf: list[np.ndarray] = []
        self._pre.clear()

    @property
    def in_speech(self) -> bool:
        return self._in_speech

    def push(self, window: np.ndarray, prob: float) -> list[tuple[Event, np.ndarray | None]]:
        events: list[tuple[Event, np.ndarray | None]] = []
        if not self._in_speech:
            self._pre.append(window)
            self._voiced_run = self._voiced_run + 1 if prob >= self.cfg.threshold else 0
            if self._voiced_run >= self._start_windows:
                self._in_speech = True
                self._silence_run = 0
                self._buf = list(self._pre)
                self._pre.clear()
                events.append((Event.SPEECH_START, None))
            return events

        self._buf.append(window)
        was_paused = self._silence_run >= self._early_windows
        self._silence_run = self._silence_run + 1 if prob < self.cfg.neg_threshold else 0
        if self._silence_run == self._early_windows and self._early_windows > 0:
            voiced_so_far = len(self._buf) - self._silence_run
            if voiced_so_far >= self._min_windows:
                events.append((Event.PAUSE, np.concatenate(self._buf)))
        elif was_paused and self._silence_run == 0:
            events.append((Event.RESUME, None))
        ended = self._silence_run >= self._end_windows
        too_long = len(self._buf) >= self._max_windows
        if ended or too_long:
            voiced = len(self._buf) - (self._silence_run if ended else 0)
            audio = np.concatenate(self._buf)
            self._in_speech = False
            self._voiced_run = 0
            self._buf = []
            if voiced >= self._min_windows:
                events.append((Event.UTTERANCE, audio))
        return events
