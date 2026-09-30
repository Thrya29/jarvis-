from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from jarvis.agent.events import EventSink
from jarvis.agent.factory import all_tools
from jarvis.agent.loop import Agent, TaskStatus
from jarvis.cli import main
from jarvis.core.config import AgentConfig, Settings
from jarvis.core.paths import AppPaths
from jarvis.llm.base import ToolCall
from jarvis.memory.store import (
    MemoryKind,
    Store,
    StoreError,
    looks_secret,
    resume_goal,
)
from jarvis.safety.policy import Approver
from jarvis.server.app import create_app
from jarvis.tools.base import ToolContext, ToolRegistry
from tests.fakes import EventLog, ScriptedApprover, ScriptedProvider, calls, make_ctx, text


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "jarvis.db")
    yield s  # type: ignore[misc]
    s.close()


# ======================================================================= store: memories


def test_remember_search_and_stemming(store: Store) -> None:
    store.remember(MemoryKind.PREFERENCE, "Prefers weekly reports as PDF")
    store.remember(MemoryKind.FACT, "Manager is Priya Sharma")
    store.remember(MemoryKind.PROJECT, "Working on the Falcon drone firmware")
    assert [m.content for m in store.search("make my report")] == ["Prefers weekly reports as PDF"]
    assert store.search("who is my manager?")[0].content == "Manager is Priya Sharma"
    assert store.search("") == []


def test_duplicate_memory_is_refreshed_not_duplicated(store: Store) -> None:
    a = store.remember(MemoryKind.FACT, "Lives in  Hyderabad")
    b = store.remember(MemoryKind.FACT, "lives in hyderabad")
    assert a.id == b.id and len(store.memories()) == 1


@pytest.mark.parametrize(
    "secret",
    [
        "My password is hunter2",
        "api key sk-ant-api03-abcdefghijklmnop",
        "card 4111 1111 1111 1111",
        "the OTP was 123456",
    ],
)
def test_secrets_are_never_stored(store: Store, secret: str) -> None:
    assert looks_secret(secret)
    with pytest.raises(StoreError, match="never stores"):
        store.remember(MemoryKind.FACT, secret)
    assert store.memories() == []


def test_update_forget_and_limits(store: Store) -> None:
    m = store.remember(MemoryKind.FACT, "Uses Excel 2019")
    assert store.update_memory(m.id, "Uses Excel 365").content == "Uses Excel 365"
    assert store.search("365")[0].id == m.id and store.search("2019") == []
    with pytest.raises(StoreError):
        store.remember(MemoryKind.FACT, "x" * 600)
    assert store.forget(m.id) and not store.forget(m.id)
    store.remember(MemoryKind.FACT, "a")
    store.remember(MemoryKind.FACT, "b")
    assert store.forget_all() == 2


def test_search_is_injection_safe(store: Store) -> None:
    store.remember(MemoryKind.FACT, "Likes tea")
    assert store.search('"); DROP TABLE memories; -- tea*')[0].content == "Likes tea"
    assert len(store.memories()) == 1


def test_context_includes_preferences_and_matches(store: Store) -> None:
    store.remember(MemoryKind.PREFERENCE, "Keep answers short")
    store.remember(MemoryKind.FACT, "Dentist is Dr. Rao")
    store.remember(MemoryKind.FACT, "Car is a Honda City")
    ctx = [m.content for m in store.context_for("book a dentist appointment")]
    assert "Keep answers short" in ctx and "Dentist is Dr. Rao" in ctx
    assert "Car is a Honda City" not in ctx
    assert len(store.context_for("anything", max_items=1)) == 1


# ======================================================================= store: workflows


def test_workflow_lifecycle(store: Store) -> None:
    wf = store.save_workflow(
        "Weekly Report",
        "Summarise the week's files",
        "Read files in {folder}, write a summary, email it to {person}.",
        ["folder", "person"],
    )
    assert wf.name == "weekly report" and wf.parameters == ["folder", "person"]
    _, rendered = store.render_workflow("weekly report", {"folder": "Docs", "person": "Priya"})
    assert rendered == "Read files in Docs, write a summary, email it to Priya."
    assert store.workflow("weekly report").run_count == 1  # type: ignore[union-attr]
    with pytest.raises(StoreError, match="needs values"):
        store.render_workflow("weekly report", {"folder": "Docs"})
    store.save_workflow("weekly report", "v2", "Just {folder}.", ["folder"])
    assert store.workflow("Weekly Report").description == "v2"  # type: ignore[union-attr]
    assert store.delete_workflow("weekly report") and store.workflows() == []


def test_workflow_validation(store: Store) -> None:
    with pytest.raises(StoreError, match="parameters not used"):
        store.save_workflow("a", "d", "No placeholders here.", ["folder"])
    with pytest.raises(StoreError, match="names"):
        store.save_workflow("../evil", "d", "x", [])
    with pytest.raises(StoreError, match="passwords"):
        store.save_workflow("login", "d", "Type password: hunter2", [])
    with pytest.raises(StoreError, match="no workflow"):
        store.render_workflow("missing", {})


# ======================================================================= store: tasks


def test_task_journal_and_resume_goal(store: Store) -> None:
    store.task_started("t1", "Organise downloads")
    store.task_plan("t1", [{"title": "Sort PDFs", "status": "done"}, {"title": "Sort images"}])
    store.task_finished("t1", "cancelled", "Cancelled by the user.", 4)
    t = store.task("t1")
    assert t is not None and t.status == "cancelled" and t.tool_calls == 4
    goal = resume_goal(t)
    assert (
        "Organise downloads" in goal and "[done] Sort PDFs" in goal and "Check the current" in goal
    )
    assert [x.id for x in store.tasks(query="downloads")] == ["t1"]


def test_mark_interrupted_only_for_dead_processes(store: Store) -> None:
    store.task_started("mine", "a")
    store.task_started("other", "b")
    store._db.execute("UPDATE tasks SET pid = 111 WHERE id = 'mine'")
    store._db.execute("UPDATE tasks SET pid = 222 WHERE id = 'other'")
    n = store.mark_interrupted(is_alive=lambda pid: pid == 222)
    assert n == 1
    assert store.task("mine").status == "interrupted"  # type: ignore[union-attr]
    assert store.task("other").status == "running"  # type: ignore[union-attr]


def test_pid_alive_self() -> None:
    import os

    from jarvis.memory.store import pid_alive

    assert pid_alive(os.getpid())
    assert not pid_alive(999_999_9)


# ======================================================================= tools


def ctx_with_store(
    tmp_path: Path, store: Store, approve: bool = True, confirm: bool = True
) -> ToolContext:
    settings = Settings(
        safety={"allowed_roots": [tmp_path / "docs"]},  # type: ignore[arg-type]
        memory={"confirm_writes": confirm},  # type: ignore[arg-type]
    )
    ctx = make_ctx(tmp_path / "docs", ScriptedApprover(approve=approve), settings=settings)
    ctx.store = store
    ctx.task_id = "current"
    return ctx


def run(ctx: ToolContext, tool: str, **args: Any) -> tuple[bool, str]:
    reg = ToolRegistry(all_tools(memory=True))
    out = asyncio.run(reg.execute(ToolCall("1", tool, args), ctx))
    return not out.is_error, out.content


def test_remember_asks_first_and_respects_decline(tmp_path: Path, store: Store) -> None:
    ctx = ctx_with_store(tmp_path, store, approve=False)
    ok, msg = run(ctx, "remember", content="Prefers dark mode", kind="preference")
    assert not ok and "declined" in msg and store.memories() == []
    approver = ctx.approver
    assert isinstance(approver, ScriptedApprover)
    assert approver.requests[0].summary == "Remember (preference): Prefers dark mode"


def test_remember_without_confirmation_when_configured(tmp_path: Path, store: Store) -> None:
    ctx = ctx_with_store(tmp_path, store, confirm=False)
    ok, _ = run(ctx, "remember", content="Prefers dark mode", kind="preference")
    assert ok and store.memories()[0].content == "Prefers dark mode"
    approver = ctx.approver
    assert isinstance(approver, ScriptedApprover) and approver.requests == []
    ok, msg = run(ctx, "recall", query="dark")
    assert ok and "Prefers dark mode" in msg


def test_forget_always_asks(tmp_path: Path, store: Store) -> None:
    m = store.remember(MemoryKind.FACT, "Old address")
    ctx = ctx_with_store(tmp_path, store, confirm=False)
    ok, _ = run(ctx, "forget", memory_id=m.id)
    approver = ctx.approver
    assert ok and isinstance(approver, ScriptedApprover) and len(approver.requests) == 1


def test_workflow_tools(tmp_path: Path, store: Store) -> None:
    ctx = ctx_with_store(tmp_path, store)
    ok, msg = run(
        ctx,
        "save_workflow",
        name="standup",
        description="Daily standup notes",
        instructions="Create standup notes for {date} in Documents.",
        parameters=["date"],
    )
    assert ok, msg
    approver = ctx.approver
    assert isinstance(approver, ScriptedApprover)
    assert "{date}" in approver.requests[-1].details  # the user sees the instructions
    ok, msg = run(ctx, "run_workflow", name="standup", arguments={"date": "Monday"})
    assert ok and "Create standup notes for Monday" in msg
    ok, msg = run(ctx, "list_workflows")
    assert ok and "standup" in msg and "run 1 times" in msg


def test_task_history_tool(tmp_path: Path, store: Store) -> None:
    store.task_started("old", "Make an Excel tracker")
    store.task_finished("old", "completed", "Saved tracker.xlsx", 3)
    store.task_started("current", "What did you do yesterday?")
    ok, msg = run(ctx_with_store(tmp_path, store), "task_history")
    assert ok and "Make an Excel tracker" in msg and "yesterday" not in msg
    assert msg.startswith("<untrusted_content")


def test_memory_disabled(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path / "docs")
    ok, msg = run(ctx, "recall", query="x")
    assert not ok and "turned off" in msg


# ======================================================================= agent integration


async def test_agent_journals_and_injects_memory(tmp_path: Path, store: Store) -> None:
    store.remember(MemoryKind.PREFERENCE, "Always name files with the date first")
    provider = ScriptedProvider(
        [
            calls(("update_plan", {"steps": [{"title": "Write file", "status": "in_progress"}]})),
            text("Done."),
        ]
    )
    ctx = ctx_with_store(tmp_path, store)
    agent = Agent(provider, ToolRegistry(all_tools(memory=True)), ctx, AgentConfig(), EventLog())
    result = await agent.run("write my notes")
    assert "# Memory" in provider.system
    first_user = provider.conversation.history[0][1]
    assert "<memory>" in first_user and "name files with the date first" in first_user
    task = store.task(result.task_id)
    assert task is not None and task.status == "completed" and task.summary == "Done."
    assert task.plan == [{"title": "Write file", "status": "in_progress"}]


async def test_cancelled_task_is_journaled(tmp_path: Path, store: Store) -> None:
    async def slow() -> Any:
        await asyncio.sleep(3600)

    ctx = ctx_with_store(tmp_path, store)
    agent = Agent(
        ScriptedProvider([slow]), ToolRegistry(all_tools(memory=True)), ctx, AgentConfig()
    )
    task = asyncio.create_task(agent.run("long thing"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.tasks()[0].status == "cancelled"


async def test_journal_failure_does_not_break_task(tmp_path: Path) -> None:
    broken = Store(tmp_path / "x.db")
    broken.close()  # every query now raises sqlite3.ProgrammingError
    ctx = ctx_with_store(tmp_path, broken)
    agent = Agent(
        ScriptedProvider([text("ok")]), ToolRegistry(all_tools(memory=True)), ctx, AgentConfig()
    )
    assert (await agent.run("x")).status is TaskStatus.COMPLETED


# ======================================================================= websocket + CLI


def test_ws_resume(tmp_path: Path, store: Store) -> None:
    store.task_started("t-old", "Sort my photos")
    store.task_finished("t-old", "interrupted", "JARVIS stopped before finishing.", 2)
    provider = ScriptedProvider([text("Finished sorting.")])

    def factory(approver: Approver, emit: EventSink) -> Agent:
        ctx = ctx_with_store(tmp_path, store)
        ctx.approver = approver
        return Agent(provider, ToolRegistry(all_tools(memory=True)), ctx, AgentConfig(), emit)

    client = TestClient(create_app(Settings(), "k" * 43, factory), base_url="http://127.0.0.1")
    with client.websocket_connect("ws://127.0.0.1/v1/ws") as ws:
        ws.send_json({"type": "auth", "token": "k" * 43})
        ws.receive_json()
        ws.send_json({"type": "task.resume", "task_id": "nope"})
        assert "no task" in ws.receive_json()["error"]
        ws.send_json({"type": "task.resume", "task_id": "t-old"})
        while ws.receive_json()["type"] != "task.finished":
            pass
    assert "Sort my photos" in provider.conversation.history[0][1]
    new = store.tasks()[0]
    assert new.resumed_from == "t-old" and new.status == "completed"


def test_cli_memory_workflows_tasks(paths: AppPaths, capsys: pytest.CaptureFixture[str]) -> None:
    s = Store(paths.data_dir / "jarvis.db")
    s.remember(MemoryKind.PREFERENCE, "Likes concise summaries")
    s.save_workflow("backup", "Back up notes", "Copy {folder} to OneDrive.", ["folder"])
    s.task_started("abc", "Do a thing")
    s.task_finished("abc", "failed", "Oops", 1)
    s.close()
    assert main(["memory", "list"]) == 0
    assert main(["workflows"]) == 0
    assert main(["tasks"]) == 0
    out = capsys.readouterr().out
    assert "Likes concise summaries" in out and "backup" in out and "(resumable)" in out
    assert main(["workflows", "run", "backup"]) == 2  # missing folder=
    assert main(["memory", "forget", "1"]) == 0
    assert "Forgotten." in capsys.readouterr().out
