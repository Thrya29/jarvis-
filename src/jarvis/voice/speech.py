"""Speech-to-text (faster-whisper) and text-to-speech (Piper, Windows SAPI fallback).

Both run on dedicated single-thread executors so CPU-heavy work never blocks the
event loop, and models load once and stay warm.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from jarvis.voice.audio import OUT_SR
from jarvis.voice.models import WHISPER_REVISIONS

log = logging.getLogger(__name__)


class Transcriber:
    def __init__(self, model: str, download_root: Path, threads: int = 0) -> None:
        if model not in WHISPER_REVISIONS:
            raise ValueError(f"unsupported speech model {model!r}; use {sorted(WHISPER_REVISIONS)}")
        self._name = model
        self._root = download_root
        self._threads = threads or max(1, min(4, (os.cpu_count() or 2) - 1))
        self._model: Any = None
        self._pool = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="stt")

    def _load(self) -> Any:
        if self._model is None:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(
                self._name,
                device="cpu",
                compute_type="int8",
                cpu_threads=self._threads,
                download_root=str(self._root),
                revision=WHISPER_REVISIONS[self._name],
            )
        return self._model

    def load(self) -> None:
        self._load()

    def _transcribe(self, audio: np.ndarray) -> str:
        model = self._load()
        segments, _ = model.transcribe(
            audio.astype(np.float32),
            language="en",
            beam_size=1,
            condition_on_previous_text=False,
            without_timestamps=True,
            vad_filter=False,
        )
        parts = []
        for s in segments:
            # Whisper hallucinates on silence/noise; drop low-confidence non-speech segments.
            if s.no_speech_prob > 0.6 and s.avg_logprob < -1.0:
                continue
            parts.append(s.text.strip())
        return " ".join(p for p in parts if p).strip()

    async def transcribe(self, audio: np.ndarray) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, self._transcribe, audio)

    async def warm_up(self) -> None:
        await self.transcribe(np.zeros(16000, dtype=np.float32))

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


class TTS(Protocol):
    async def synthesize(self, text: str) -> np.ndarray:
        """float32 mono audio at OUT_SR."""
        ...

    def close(self) -> None: ...


def _to_out_sr(audio: np.ndarray, sr: int) -> np.ndarray:
    if sr == OUT_SR:
        return audio.astype(np.float32)
    import soxr

    return np.asarray(soxr.resample(audio.astype(np.float32), sr, OUT_SR), dtype=np.float32)


class PiperTTS:
    def __init__(self, model_path: Path, speed: float = 1.0) -> None:
        self._path = model_path
        self._speed = speed
        self._voice: Any = None
        self._pool = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="tts")

    def _synth(self, text: str) -> np.ndarray:
        from piper import PiperVoice, SynthesisConfig

        if self._voice is None:
            self._voice = PiperVoice.load(str(self._path))
        cfg = SynthesisConfig(length_scale=1.0 / max(0.5, min(2.0, self._speed)))
        chunks = list(self._voice.synthesize(text, syn_config=cfg))
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        audio = np.concatenate([c.audio_float_array for c in chunks])
        return _to_out_sr(audio, chunks[0].sample_rate)

    async def synthesize(self, text: str) -> np.ndarray:
        return await asyncio.get_running_loop().run_in_executor(self._pool, self._synth, text)

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


class SapiTTS:
    """Windows' built-in voices: no download, instant, lower quality."""

    SAFT22K16BIT_MONO = 22

    def __init__(self, speed: float = 1.0) -> None:
        self._rate = int(max(-10, min(10, round((speed - 1.0) * 10))))
        self._pool = concurrent.futures.ThreadPoolExecutor(
            1, thread_name_prefix="sapi", initializer=self._init_com
        )
        self._voice: Any = None

    @staticmethod
    def _init_com() -> None:
        import pythoncom

        pythoncom.CoInitialize()

    def _synth(self, text: str) -> np.ndarray:
        import win32com.client

        if self._voice is None:
            self._voice = win32com.client.Dispatch("SAPI.SpVoice")
            self._voice.Rate = self._rate
        stream = win32com.client.Dispatch("SAPI.SpMemoryStream")
        fmt = win32com.client.Dispatch("SAPI.SpAudioFormat")
        fmt.Type = self.SAFT22K16BIT_MONO
        stream.Format = fmt
        self._voice.AudioOutputStream = stream
        self._voice.Speak(text)
        data = bytes(stream.GetData())
        pcm = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        return _to_out_sr(pcm, 22050)

    async def synthesize(self, text: str) -> np.ndarray:
        return await asyncio.get_running_loop().run_in_executor(self._pool, self._synth, text)

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


def tone(freq: float = 880.0, ms: int = 120, volume: float = 0.2) -> np.ndarray:
    """Short acknowledgement chime (with fade in/out to avoid clicks)."""
    n = int(OUT_SR * ms / 1000)
    t = np.arange(n) / OUT_SR
    wave = np.sin(2 * np.pi * freq * t) * volume
    fade = min(n // 4, int(OUT_SR * 0.01))
    env = np.ones(n)
    env[:fade] = np.linspace(0, 1, fade)
    env[-fade:] = np.linspace(1, 0, fade)
    return (wave * env).astype(np.float32)
