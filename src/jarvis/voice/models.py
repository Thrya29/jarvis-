"""Voice model files: pinned versions, verified downloads, local cache.

Every file is pinned to an exact release/commit and checked against a SHA-256 hash
after download, so a compromised or changed upstream file is rejected rather than
loaded into the process.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx2 as httpx

log = logging.getLogger(__name__)

OWW = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1"
PIPER_REV = "c10ece1aade47bb51c153c893d14e5bf8e5b7117"
PIPER = f"https://huggingface.co/rhasspy/piper-voices/resolve/{PIPER_REV}/en/en_US"

# faster-whisper models come from Hugging Face at a pinned commit.
WHISPER_REVISIONS = {
    "tiny.en": None,
    "base.en": "3d3d5dee26484f91867d81cb899cfcf72b96be6c",
    "small.en": "d1d751a5f8271d482d14ca55d9e2deeebbae577f",
}


@dataclass(frozen=True)
class ModelFile:
    name: str
    url: str
    sha256: str
    size: int


FILES: dict[str, ModelFile] = {
    f.name: f
    for f in [
        ModelFile(
            "melspectrogram.onnx",
            f"{OWW}/melspectrogram.onnx",
            "ba2b0e0f8b7b875369a2c89cb13360ff53bac436f2895cced9f479fa65eb176f",
            1_087_958,
        ),
        ModelFile(
            "embedding_model.onnx",
            f"{OWW}/embedding_model.onnx",
            "70d164290c1d095d1d4ee149bc5e00543250a7316b59f31d056cff7bd3075c1f",
            1_326_578,
        ),
        ModelFile(
            "hey_jarvis_v0.1.onnx",
            f"{OWW}/hey_jarvis_v0.1.onnx",
            "94a13cfe60075b132f6a472e7e462e8123ee70861bc3fb58434a73712ee0d2cb",
            1_271_370,
        ),
        ModelFile(
            "en_US-lessac-medium.onnx",
            f"{PIPER}/lessac/medium/en_US-lessac-medium.onnx",
            "5efe09e69902187827af646e1a6e9d269dee769f9877d17b16b1b46eeaaf019f",
            63_201_294,
        ),
        ModelFile(
            "en_US-lessac-medium.onnx.json",
            f"{PIPER}/lessac/medium/en_US-lessac-medium.onnx.json",
            "efe19c417bed055f2d69908248c6ba650fa135bc868b0e6abb3da181dab690a0",
            4_885,
        ),
        ModelFile(
            "en_US-amy-medium.onnx",
            f"{PIPER}/amy/medium/en_US-amy-medium.onnx",
            "b3a6e47b57b8c7fbe6a0ce2518161a50f59a9cdd8a50835c02cb02bdd6206c18",
            63_201_294,
        ),
        ModelFile(
            "en_US-amy-medium.onnx.json",
            f"{PIPER}/amy/medium/en_US-amy-medium.onnx.json",
            "95a23eb4d42909d38df73bb9ac7f45f597dbfcde2d1bf9526fdeaf5466977d77",
            4_882,
        ),
    ]
}

WAKE_FILES = ["melspectrogram.onnx", "embedding_model.onnx", "hey_jarvis_v0.1.onnx"]
PIPER_VOICES = {"lessac": "en_US-lessac-medium", "amy": "en_US-amy-medium"}


class ModelError(RuntimeError):
    pass


def piper_files(voice: str) -> list[str]:
    stem = PIPER_VOICES.get(voice)
    if stem is None:
        raise ModelError(f"unknown Piper voice {voice!r}; choose one of {sorted(PIPER_VOICES)}")
    return [f"{stem}.onnx", f"{stem}.onnx.json"]


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


class ModelStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, name: str) -> Path:
        return self.root / name

    def has(self, name: str) -> bool:
        f = FILES[name]
        p = self.path(name)
        return p.is_file() and p.stat().st_size == f.size

    def missing(self, names: list[str]) -> list[str]:
        return [n for n in names if not self.has(n)]

    def fetch(
        self,
        names: list[str],
        progress: Callable[[str, int, int], None] | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        own = client is None
        http = client or httpx.Client(follow_redirects=True, timeout=60)
        try:
            for name in self.missing(names):
                self._download(http, FILES[name], progress)
        finally:
            if own:
                http.close()

    def _download(
        self,
        http: httpx.Client,
        f: ModelFile,
        progress: Callable[[str, int, int], None] | None,
    ) -> None:
        tmp = self.path(f.name + ".part")
        h = hashlib.sha256()
        done = 0
        log.info("downloading %s", f.url)
        with http.stream("GET", f.url) as resp:
            if resp.status_code != 200:
                raise ModelError(f"download of {f.name} failed: HTTP {resp.status_code}")
            with tmp.open("wb") as out:
                for chunk in resp.iter_bytes():
                    out.write(chunk)
                    h.update(chunk)
                    done += len(chunk)
                    if progress:
                        progress(f.name, done, f.size)
                    if done > f.size:
                        break
        if h.hexdigest() != f.sha256 or done != f.size:
            tmp.unlink(missing_ok=True)
            raise ModelError(
                f"{f.name} failed verification (sha256 or size mismatch); refusing to use it"
            )
        tmp.replace(self.path(f.name))

    def whisper_dir(self) -> Path:
        return self.root / "whisper"
