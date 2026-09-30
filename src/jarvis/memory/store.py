"""Long-term memory, saved workflows and the task journal, in one local SQLite file.

- Memories: short facts and preferences about the user, searchable with SQLite FTS5
  (BM25 ranking, Porter stemming). For a personal store of hundreds of entries this is
  accurate and needs no embedding model.
- Workflows: named, parameterised procedures the user asked JARVIS to save.
- Tasks: every goal JARVIS worked on, with status, plan and summary, so interrupted
  tasks can be resumed and "what did you do yesterday?" can be answered.

The database lives in the per-user data folder, which tools can never reach.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    source_task TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content, content='memories', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content) VALUES ('delete', old.id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content) VALUES ('delete', old.id, old.content);
    INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TABLE IF NOT EXISTS workflows (
    name TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    instructions TEXT NOT NULL,
    parameters TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_run_at TEXT,
    run_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    goal TEXT NOT NULL,
    status TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    plan TEXT NOT NULL DEFAULT '[]',
    tool_calls INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    resumed_from TEXT,
    pid INTEGER
);
CREATE INDEX IF NOT EXISTS tasks_started ON tasks(started_at DESC);
"""

MAX_MEMORY_CHARS = 500
MAX_MEMORIES = 2000
_SECRET_HINTS = re.compile(
    r"(sk-ant-|sk-[A-Za-z0-9]{20,}|\bpassword\b\s*(is|:|=)|\bpasscode\b|\bpin\b\s*(is|:|=)|"
    r"\botp\b|\bcvv\b|\b(?:\d[ -]?){13,19}\b|-----BEGIN [A-Z ]*PRIVATE KEY)",
    re.IGNORECASE,
)
_NAME = re.compile(r"^[a-z0-9][a-z0-9 _-]{0,59}$")


class MemoryKind(StrEnum):
    PREFERENCE = "preference"  # how the user likes things done
    FACT = "fact"  # facts about the user, their work, people, places
    PROJECT = "project"  # ongoing work and its context


class StoreError(ValueError):
    """Invalid memory/workflow operation (reported back to the model)."""


@dataclass(frozen=True)
class Memory:
    id: int
    kind: MemoryKind
    content: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Workflow:
    name: str
    description: str
    instructions: str
    parameters: list[str]
    run_count: int
    last_run_at: str | None


@dataclass(frozen=True)
class TaskRecord:
    id: str
    goal: str
    status: str
    summary: str
    plan: list[dict[str, Any]]
    tool_calls: int
    started_at: str
    finished_at: str | None
    resumed_from: str | None


RESUMABLE = {"interrupted", "cancelled", "failed", "limit_reached"}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def pid_alive(pid: int) -> bool:
    if pid == os.getpid():
        return True
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32")
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def looks_secret(text: str) -> bool:
    return bool(_SECRET_HINTS.search(text))


def _fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 OR-query of quoted terms."""
    words = re.findall(r"[\w']{2,}", text.lower())
    stop = {"the", "and", "for", "you", "your", "with", "that", "this", "what", "are", "was"}
    terms = [w.replace("'", "") for w in words if w not in stop][:12]
    return " OR ".join(f'"{t}"' for t in terms if t)


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(SCHEMA)
        self._db.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------ memories

    def remember(self, kind: MemoryKind, content: str, source_task: str | None = None) -> Memory:
        content = " ".join(content.split())
        if not content:
            raise StoreError("memory content is empty")
        if len(content) > MAX_MEMORY_CHARS:
            raise StoreError(f"keep memories under {MAX_MEMORY_CHARS} characters")
        if looks_secret(content):
            raise StoreError(
                "that looks like a password, key, card number or code; JARVIS never stores those"
            )
        with self._tx() as db:
            dup = db.execute(
                "SELECT id FROM memories WHERE lower(content) = lower(?)", (content,)
            ).fetchone()
            if dup:
                db.execute(
                    "UPDATE memories SET updated_at = ?, kind = ? WHERE id = ?",
                    (_now(), kind.value, dup["id"]),
                )
                mid = int(dup["id"])
            else:
                count = db.execute("SELECT count(*) FROM memories").fetchone()[0]
                if count >= MAX_MEMORIES:
                    raise StoreError("memory is full; forget something first")
                now = _now()
                cur = db.execute(
                    "INSERT INTO memories(kind, content, created_at, updated_at, source_task) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (kind.value, content, now, now, source_task),
                )
                mid = int(cur.lastrowid or 0)
        mem = self.get_memory(mid)
        assert mem is not None
        return mem

    def get_memory(self, mid: int) -> Memory | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM memories WHERE id = ?", (mid,)).fetchone()
        return _memory(row) if row else None

    def update_memory(self, mid: int, content: str) -> Memory:
        content = " ".join(content.split())
        if not content or len(content) > MAX_MEMORY_CHARS or looks_secret(content):
            raise StoreError("invalid memory content")
        with self._tx() as db:
            cur = db.execute(
                "UPDATE memories SET content = ?, updated_at = ? WHERE id = ?",
                (content, _now(), mid),
            )
            if cur.rowcount == 0:
                raise StoreError(f"no memory with id {mid}")
        mem = self.get_memory(mid)
        assert mem is not None
        return mem

    def forget(self, mid: int) -> bool:
        with self._tx() as db:
            return db.execute("DELETE FROM memories WHERE id = ?", (mid,)).rowcount > 0

    def forget_all(self) -> int:
        with self._tx() as db:
            return db.execute("DELETE FROM memories").rowcount

    def memories(self, kind: MemoryKind | None = None, limit: int = 500) -> list[Memory]:
        q = "SELECT * FROM memories"
        args: tuple[Any, ...] = ()
        if kind:
            q += " WHERE kind = ?"
            args = (kind.value,)
        q += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        with self._lock:
            rows = self._db.execute(q, (*args, limit)).fetchall()
        return [_memory(r) for r in rows]

    def search(self, text: str, limit: int = 8) -> list[Memory]:
        query = _fts_query(text)
        if not query:
            return []
        with self._lock:
            rows = self._db.execute(
                "SELECT m.* FROM memories_fts f JOIN memories m ON m.id = f.rowid "
                "WHERE memories_fts MATCH ? ORDER BY bm25(memories_fts), m.updated_at DESC "
                "LIMIT ?",
                (query, limit),
            ).fetchall()
        return [_memory(r) for r in rows]

    def context_for(self, goal: str, max_items: int = 12, max_chars: int = 2500) -> list[Memory]:
        """Memories worth showing the model for this goal: all preferences, plus matches."""
        chosen: dict[int, Memory] = {}
        for m in self.memories(MemoryKind.PREFERENCE, limit=max_items):
            chosen[m.id] = m
        for m in self.search(goal, limit=max_items):
            chosen.setdefault(m.id, m)
        out: list[Memory] = []
        size = 0
        for m in chosen.values():
            if len(out) >= max_items or size + len(m.content) > max_chars:
                break
            out.append(m)
            size += len(m.content)
        return out

    # ------------------------------------------------------------------ workflows

    def save_workflow(
        self, name: str, description: str, instructions: str, parameters: list[str]
    ) -> Workflow:
        name = name.strip().lower()
        if not _NAME.match(name):
            raise StoreError("workflow names use letters, digits, spaces, - or _ (max 60)")
        if not instructions.strip():
            raise StoreError("workflow instructions are empty")
        if looks_secret(instructions):
            raise StoreError("workflows must not contain passwords, keys or codes")
        params = sorted({p.strip() for p in parameters if p.strip()})
        missing = [p for p in params if "{" + p + "}" not in instructions]
        if missing:
            raise StoreError(f"parameters not used as {{name}} in the instructions: {missing}")
        now = _now()
        with self._tx() as db:
            db.execute(
                "INSERT INTO workflows(name, description, instructions, parameters, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET "
                "description = excluded.description, instructions = excluded.instructions, "
                "parameters = excluded.parameters, updated_at = excluded.updated_at",
                (name, description.strip(), instructions.strip(), json.dumps(params), now, now),
            )
        wf = self.workflow(name)
        assert wf is not None
        return wf

    def workflow(self, name: str) -> Workflow | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM workflows WHERE name = ?", (name.strip().lower(),)
            ).fetchone()
        return _workflow(row) if row else None

    def workflows(self) -> list[Workflow]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM workflows ORDER BY name").fetchall()
        return [_workflow(r) for r in rows]

    def delete_workflow(self, name: str) -> bool:
        with self._tx() as db:
            return (
                db.execute("DELETE FROM workflows WHERE name = ?", (name.strip().lower(),)).rowcount
                > 0
            )

    def render_workflow(self, name: str, arguments: dict[str, str]) -> tuple[Workflow, str]:
        wf = self.workflow(name)
        if wf is None:
            known = ", ".join(w.name for w in self.workflows()) or "none saved yet"
            raise StoreError(f"no workflow named {name!r} (saved: {known})")
        missing = [p for p in wf.parameters if not str(arguments.get(p, "")).strip()]
        if missing:
            raise StoreError(f"workflow {wf.name!r} needs values for: {missing}")
        text = wf.instructions
        for p in wf.parameters:
            text = text.replace("{" + p + "}", str(arguments[p]))
        with self._tx() as db:
            db.execute(
                "UPDATE workflows SET run_count = run_count + 1, last_run_at = ? WHERE name = ?",
                (_now(), wf.name),
            )
        return wf, text

    # ------------------------------------------------------------------ task journal

    def task_started(self, task_id: str, goal: str, resumed_from: str | None = None) -> None:
        with self._tx() as db:
            db.execute(
                "INSERT INTO tasks(id, goal, status, started_at, resumed_from, pid) "
                "VALUES (?, ?, 'running', ?, ?, ?)",
                (task_id, goal, _now(), resumed_from, os.getpid()),
            )

    def task_plan(self, task_id: str, steps: list[dict[str, Any]]) -> None:
        with self._tx() as db:
            db.execute("UPDATE tasks SET plan = ? WHERE id = ?", (json.dumps(steps), task_id))

    def task_finished(self, task_id: str, status: str, summary: str, tool_calls: int) -> None:
        with self._tx() as db:
            db.execute(
                "UPDATE tasks SET status = ?, summary = ?, tool_calls = ?, finished_at = ? "
                "WHERE id = ?",
                (status, summary[:4000], tool_calls, _now(), task_id),
            )

    def mark_interrupted(self, is_alive: Callable[[int], bool] | None = None) -> int:
        """At startup: 'running' tasks whose JARVIS process is gone were interrupted.

        Tasks owned by another live JARVIS process (e.g. the daemon while the CLI starts)
        are left alone.
        """
        alive = is_alive or pid_alive
        with self._tx() as db:
            rows = db.execute("SELECT id, pid FROM tasks WHERE status = 'running'").fetchall()
            dead = [r["id"] for r in rows if not r["pid"] or not alive(int(r["pid"]))]
            for tid in dead:
                db.execute(
                    "UPDATE tasks SET status = 'interrupted', finished_at = ?, "
                    "summary = CASE WHEN summary = '' THEN 'JARVIS stopped before finishing.' "
                    "ELSE summary END WHERE id = ?",
                    (_now(), tid),
                )
            return len(dead)

    def task(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return _task(row) if row else None

    def tasks(self, limit: int = 20, query: str | None = None) -> list[TaskRecord]:
        with self._lock:
            if query:
                like = f"%{query.lower()}%"
                rows = self._db.execute(
                    "SELECT * FROM tasks WHERE lower(goal) LIKE ? OR lower(summary) LIKE ? "
                    "ORDER BY started_at DESC, rowid DESC LIMIT ?",
                    (like, like, limit),
                ).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT * FROM tasks ORDER BY started_at DESC, rowid DESC LIMIT ?", (limit,)
                ).fetchall()
        return [_task(r) for r in rows]


def _memory(r: sqlite3.Row) -> Memory:
    return Memory(r["id"], MemoryKind(r["kind"]), r["content"], r["created_at"], r["updated_at"])


def _workflow(r: sqlite3.Row) -> Workflow:
    return Workflow(
        r["name"],
        r["description"],
        r["instructions"],
        json.loads(r["parameters"]),
        r["run_count"],
        r["last_run_at"],
    )


def _task(r: sqlite3.Row) -> TaskRecord:
    return TaskRecord(
        r["id"],
        r["goal"],
        r["status"],
        r["summary"],
        json.loads(r["plan"]),
        r["tool_calls"],
        r["started_at"],
        r["finished_at"],
        r["resumed_from"],
    )


def resume_goal(t: TaskRecord) -> str:
    """The goal text used to resume an unfinished task."""
    lines = [
        "Resume a task that did not finish.",
        f"Original request: {t.goal}",
        f"It ended as '{t.status}' ({t.finished_at or 'unknown time'}): "
        f"{t.summary or 'no summary'}",
    ]
    if t.plan:
        steps = "; ".join(f"[{s.get('status', '?')}] {s.get('title', '')}" for s in t.plan)
        lines.append(f"Plan at that point: {steps}")
    lines.append(
        "Some steps may already be done. Check the current state (files, windows) before "
        "repeating anything, then finish the remaining work."
    )
    return "\n".join(lines)
