"""Ask my documents: a local, incremental, hybrid search index over the user's files.

- Text is extracted (txt/md/docx/pdf/xlsx/...), split into overlapping chunks and embedded
  with BGE-small (ONNX, int8, runs on CPU).
- Search fuses vector similarity with SQLite FTS5 keyword ranking (reciprocal-rank
  fusion), so meaning-based questions and exact names/numbers both work.
- Indexing is incremental (by size + modified time) and bounded in time per call, so a
  first run over many files spreads across calls instead of blocking.
- Only folders inside the allowed roots are indexed; the database lives in JARVIS's own
  data folder, which tools can't reach.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from jarvis.safety.policy import PathDeniedError, PathGuard
from jarvis.tools.readers import TEXT_SUFFIXES, extract_text

log = logging.getLogger(__name__)

INDEXABLE = (TEXT_SUFFIXES - {".rtf"}) | {".docx", ".pdf", ".xlsx"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "$RECYCLE.BIN", "AppData"}
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TEXT_CHARS = 1_000_000
CHUNK_CHARS = 1200
OVERLAP_CHARS = 150
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, size INTEGER, mtime REAL,
    indexed_at REAL, error TEXT
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    ord INTEGER NOT NULL, text TEXT NOT NULL, vec BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_file ON chunks(file_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, content='chunks', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
"""


class Embedder:
    """BGE-small-en-v1.5 (int8 ONNX) sentence embeddings, CLS-pooled and normalised."""

    def __init__(self, model: Path, tokenizer: Path, threads: int = 0) -> None:
        self._model_path, self._tok_path = model, tokenizer
        self._threads = threads or max(1, min(4, (os.cpu_count() or 2) - 1))
        self._session: Any = None
        self._tok: Any = None
        self._lock = threading.Lock()

    def _load(self) -> None:
        if self._session is None:
            import onnxruntime as ort
            from tokenizers import Tokenizer

            opts = ort.SessionOptions()
            opts.intra_op_num_threads = self._threads
            opts.log_severity_level = 3
            self._session = ort.InferenceSession(
                str(self._model_path), sess_options=opts, providers=["CPUExecutionProvider"]
            )
            tok = Tokenizer.from_file(str(self._tok_path))
            tok.enable_truncation(max_length=320)
            tok.enable_padding()
            self._tok = tok

    def embed(self, texts: list[str], batch: int = 16) -> np.ndarray:
        with self._lock:
            self._load()
            out = []
            for i in range(0, len(texts), batch):
                enc = self._tok.encode_batch(texts[i : i + batch])
                ids = np.array([e.ids for e in enc], dtype=np.int64)
                mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
                hidden = self._session.run(
                    None,
                    {
                        "input_ids": ids,
                        "attention_mask": mask,
                        "token_type_ids": np.zeros_like(ids),
                    },
                )[0]
                v = hidden[:, 0]
                out.append(v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-9))
            return np.vstack(out).astype(np.float32) if out else np.zeros((0, 384), np.float32)


def chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = OVERLAP_CHARS) -> list[str]:
    """Split on paragraph boundaries into ~size-char chunks with some overlap."""
    paras = [p.strip() for p in text.replace("\r", "").split("\n\n") if p.strip()]
    chunks: list[str] = []
    cur = ""
    for p in paras:
        while len(p) > size:  # very long paragraph: hard split
            head, p = p[:size], p[size - overlap :]
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(head)
        if len(cur) + len(p) + 2 <= size:
            cur = f"{cur}\n\n{p}" if cur else p
        else:
            if cur:
                chunks.append(cur)
            tail = cur[-overlap:] if cur else ""
            cur = f"{tail}\n\n{p}" if tail else p
    if cur:
        chunks.append(cur)
    return chunks


@dataclass(frozen=True)
class Hit:
    path: str
    chunk: int
    text: str
    score: float


@dataclass
class SyncReport:
    indexed: int = 0
    removed: int = 0
    unchanged: int = 0
    failed: int = 0
    pending: int = 0  # files left for a later call (time budget reached)
    skipped_folders: tuple[str, ...] = ()


class DocIndex:
    def __init__(self, db_path: Path, embedder: Embedder) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(SCHEMA)
        self._embedder = embedder
        self._lock = threading.RLock()
        self._matrix: np.ndarray | None = None  # cached vectors for search
        self._ids: np.ndarray | None = None
        self.last_sync = 0.0

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------ indexing

    def _discover(self, folders: list[Path], guard: PathGuard) -> tuple[list[Path], list[str]]:
        files: list[Path] = []
        skipped: list[str] = []
        for folder in folders:
            try:
                root = guard.resolve(folder)
            except PathDeniedError:
                skipped.append(str(folder))
                continue
            if not root.is_dir():
                skipped.append(str(folder))
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
                for name in filenames:
                    p = Path(dirpath) / name
                    if p.suffix.lower() in INDEXABLE and not name.startswith("~$"):
                        files.append(p)
        return files, skipped

    def sync(
        self,
        folders: list[Path],
        guard: PathGuard,
        time_budget_s: float = 60.0,
        progress: Callable[[int, int], None] | None = None,
    ) -> SyncReport:
        """Bring the index up to date (within a time budget)."""
        report = SyncReport()
        files, skipped = self._discover(folders, guard)
        report.skipped_folders = tuple(skipped)
        present = {str(p) for p in files}
        deadline = time.monotonic() + time_budget_s
        with self._lock:
            known = {
                r[0]: (r[1], r[2], r[3])
                for r in self._db.execute("SELECT path, size, mtime, id FROM files")
            }
            for path in [p for p in known if p not in present]:
                self._db.execute("DELETE FROM files WHERE path = ?", (path,))
                report.removed += 1
        todo = []
        for p in files:
            try:
                st = p.stat()
            except OSError:
                continue
            prev = known.get(str(p))
            if prev and prev[0] == st.st_size and abs((prev[1] or 0) - st.st_mtime) < 1e-6:
                report.unchanged += 1
            elif st.st_size <= MAX_FILE_BYTES:
                todo.append((p, st))
        for i, (p, st) in enumerate(todo):
            if time.monotonic() > deadline:
                report.pending = len(todo) - i
                break
            if progress:
                progress(i, len(todo))
            try:
                self._index_file(p, st.st_size, st.st_mtime)
                report.indexed += 1
            except Exception as exc:  # unreadable/corrupt files must not stop indexing
                log.info("could not index %s: %s", p, exc)
                with self._lock:
                    self._db.execute(
                        "INSERT INTO files(path, size, mtime, indexed_at, error) "
                        "VALUES (?,?,?,?,?) "
                        "ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime=excluded.mtime, "
                        "indexed_at=excluded.indexed_at, error=excluded.error",
                        (str(p), st.st_size, st.st_mtime, time.time(), str(exc)[:200]),
                    )
                report.failed += 1
        with self._lock:
            self._matrix = None  # reload vectors on next search
        self.last_sync = time.time()
        return report

    def _index_file(self, path: Path, size: int, mtime: float) -> None:
        text = extract_text(path, MAX_TEXT_CHARS)
        pieces = chunk_text(text)
        header = f"{path.name}\n"
        vecs = self._embedder.embed([header + c for c in pieces]) if pieces else None
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute("DELETE FROM files WHERE path = ?", (str(path),))
                cur = self._db.execute(
                    "INSERT INTO files(path, size, mtime, indexed_at) VALUES (?,?,?,?)",
                    (str(path), size, mtime, time.time()),
                )
                fid = cur.lastrowid
                for i, piece in enumerate(pieces):
                    assert vecs is not None
                    self._db.execute(
                        "INSERT INTO chunks(file_id, ord, text, vec) VALUES (?,?,?,?)",
                        (fid, i, piece, vecs[i].astype(np.float16).tobytes()),
                    )
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    # ------------------------------------------------------------------ search

    def stats(self) -> dict[str, Any]:
        with self._lock:
            files = self._db.execute("SELECT count(*) FROM files WHERE error IS NULL").fetchone()[0]
            failed = self._db.execute(
                "SELECT count(*) FROM files WHERE error IS NOT NULL"
            ).fetchone()[0]
            chunks = self._db.execute("SELECT count(*) FROM chunks").fetchone()[0]
        return {"files": files, "failed": failed, "chunks": chunks, "last_sync": self.last_sync}

    def _vectors(self) -> tuple[np.ndarray, np.ndarray]:
        with self._lock:
            if self._matrix is None:
                rows = self._db.execute("SELECT id, vec FROM chunks").fetchall()
                self._ids = np.array([r[0] for r in rows], dtype=np.int64)
                self._matrix = (
                    np.vstack([np.frombuffer(r[1], dtype=np.float16) for r in rows]).astype(
                        np.float32
                    )
                    if rows
                    else np.zeros((0, 384), np.float32)
                )
            assert self._ids is not None
            return self._ids, self._matrix

    def search(self, query: str, k: int = 6) -> list[Hit]:
        ids, matrix = self._vectors()
        if not len(ids):
            return []
        q = self._embedder.embed([QUERY_PREFIX + query])[0]
        sims = matrix @ q
        vec_rank = {int(ids[i]): r for r, i in enumerate(np.argsort(-sims)[: k * 4])}
        kw_rank: dict[int, int] = {}
        terms = [
            t
            for t in "".join(c if c.isalnum() else " " for c in query.lower()).split()
            if len(t) > 2
        ]
        if terms:
            fts = " OR ".join(f'"{t}"' for t in terms[:12])
            with self._lock:
                rows = self._db.execute(
                    "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? "
                    "ORDER BY bm25(chunks_fts) LIMIT ?",
                    (fts, k * 4),
                ).fetchall()
            kw_rank = {int(r[0]): i for i, r in enumerate(rows)}
        fused: dict[int, float] = {}
        for rank_map in (vec_rank, kw_rank):
            for cid, r in rank_map.items():
                fused[cid] = fused.get(cid, 0.0) + 1.0 / (60 + r)
        best = sorted(fused, key=fused.__getitem__, reverse=True)[:k]
        hits = []
        with self._lock:
            for cid in best:
                row = self._db.execute(
                    "SELECT f.path, c.ord, c.text FROM chunks c "
                    "JOIN files f ON f.id = c.file_id WHERE c.id = ?",
                    (cid,),
                ).fetchone()
                if row:
                    hits.append(Hit(row[0], row[1], row[2], round(fused[cid], 4)))
        return hits
