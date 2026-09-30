"""Wake-word detection ("Hey Jarvis") with openWakeWord's ONNX models.

A faithful streaming re-implementation of openWakeWord's inference path
(mel-spectrogram -> speech embedding -> classifier) using only onnxruntime, so the
package's training-only dependencies (scipy, scikit-learn) aren't needed.

Audio: 16 kHz int16, consumed in 80 ms (1280-sample) steps.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import onnxruntime as ort

CHUNK = 1280  # 80 ms at 16 kHz
MEL_CONTEXT = 160 * 3  # extra samples so each step yields 8 fresh mel frames
MEL_WINDOW = 76  # mel frames per embedding
MEL_MAX = 970
FEATURE_WINDOW = 16  # embeddings per classification (~1.3 s)
FEATURE_MAX = 120
WARMUP_STEPS = 5  # openWakeWord ignores the first predictions after a reset


def _session(path: Path) -> ort.InferenceSession:
    opts = ort.SessionOptions()
    opts.inter_op_num_threads = 1
    opts.intra_op_num_threads = 1
    opts.log_severity_level = 3
    return ort.InferenceSession(str(path), sess_options=opts, providers=["CPUExecutionProvider"])


class WakeWordDetector:
    def __init__(self, melspec: Path, embedding: Path, classifier: Path, threshold: float = 0.5):
        self._mel = _session(melspec)
        self._emb = _session(embedding)
        self._clf = _session(classifier)
        self._clf_input = self._clf.get_inputs()[0].name
        self.threshold = threshold
        self.reset()

    # -------------------------------------------------------------- model steps

    def _melspec(self, audio: np.ndarray) -> np.ndarray:
        x = audio.astype(np.float32)[None, :]
        spec = np.squeeze(np.asarray(self._mel.run(None, {"input": x})[0], dtype=np.float32))
        return spec / 10.0 + 2.0  # transform used by openWakeWord to match the TF original

    def _embed(self, windows: np.ndarray) -> np.ndarray:
        out = np.asarray(self._emb.run(None, {"input_1": windows.astype(np.float32)})[0])
        return out.reshape(windows.shape[0], -1).astype(np.float32)

    def reset(self) -> None:
        self._raw = np.zeros(0, dtype=np.int16)
        self._pending = np.zeros(0, dtype=np.int16)
        self._mel_buf = np.ones((MEL_WINDOW, 32), dtype=np.float32)
        # Seed the feature history like openWakeWord does (embeddings of low-level noise).
        rng = np.random.default_rng(0)
        noise = rng.integers(-1000, 1000, 16000 * 4).astype(np.int16)
        spec = self._melspec(noise)
        wins = [spec[i : i + MEL_WINDOW] for i in range(0, spec.shape[0], 8)]
        wins = [w for w in wins if w.shape[0] == MEL_WINDOW]
        self._features = self._embed(np.stack(wins)[..., None])
        self._steps = 0
        self.last_score = 0.0

    # -------------------------------------------------------------- streaming

    def process(self, pcm: np.ndarray) -> bool:
        """Feed int16 audio of any length; True if the wake word was heard."""
        self._pending = np.concatenate([self._pending, pcm.astype(np.int16)])
        fired = False
        while self._pending.shape[0] >= CHUNK:
            chunk, self._pending = self._pending[:CHUNK], self._pending[CHUNK:]
            if self._step(chunk):
                fired = True
        return fired

    def _step(self, chunk: np.ndarray) -> bool:
        self._raw = np.concatenate([self._raw, chunk])[-(CHUNK + MEL_CONTEXT) :]
        mel = self._melspec(self._raw)
        self._mel_buf = np.vstack([self._mel_buf, mel])[-MEL_MAX:]
        window = self._mel_buf[-MEL_WINDOW:][None, :, :, None]
        self._features = np.vstack([self._features, self._embed(window)])[-FEATURE_MAX:]
        x = self._features[-FEATURE_WINDOW:][None, :, :].astype(np.float32)
        score = float(np.asarray(self._clf.run(None, {self._clf_input: x})[0]).reshape(-1)[0])
        self._steps += 1
        if self._steps <= WARMUP_STEPS:
            score = 0.0
        self.last_score = score
        return score >= self.threshold
