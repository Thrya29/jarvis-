from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from jarvis.agent.factory import all_tools
from jarvis.agent.loop import Agent, TaskStatus
from jarvis.core.config import AgentConfig
from jarvis.llm.base import LLMError, StopKind, TurnResult
from jarvis.tools.base import ToolRegistry
from tests.fakes import EventLog, ScriptedApprover, ScriptedProvider, calls, make_ctx, text


def make_agent(
    root: Path, script: list, approver: ScriptedApprover | None = None, **cfg: float
) -> tuple[Agent, ScriptedProvider, EventLog]:
    provider = ScriptedProvider(script)
    events = EventLog()
    agent = Agent(
        provider,
        ToolRegistry(all_tools()),
        make_ctx(root, approver),
        AgentConfig(**cfg),  # type: ignore[arg-type]
        events,
    )
    return agent, provider, events


async def test_plan_act_verify_complete(tmp_path: Path) -> None:
    root = tmp_path / "docs"
    agent, provider, events = make_agent(
        root,
        [
            calls(("update_plan", {"steps": [{"title": "Write notes", "status": "in_progress"}]})),
            calls(("write_file", {"path": "notes.txt", "content": "hi"})),
            calls(("read_file", {"path": "notes.txt"})),
            text("Created notes.txt"),
        ],
    )
    result = await agent.run("make notes")
    assert result.status is TaskStatus.COMPLETED
    assert result.summary == "Created notes.txt"
    assert result.tool_calls == 3
    assert (root / "notes.txt").read_text(encoding="utf-8") == "hi"
    assert events.types()[0] == "task.started" and events.types()[-1] == "task.finished"
    assert "plan.updated" in events.types()
    assert "Folders you may access" in provider.system
    first_user = provider.conversation.history[0][1]
    assert first_user.startswith("make notes") and "<context>" in first_user


async def test_follow_up_goals_share_conversation(tmp_path: Path) -> None:
    agent, provider, _ = make_agent(tmp_path / "d", [text("one"), text("two")])
    await agent.run("first")
    await agent.run("second")
    users = [h for h in provider.conversation.history if h[0] == "user"]
    assert len(users) == 2


async def test_refusal(tmp_path: Path) -> None:
    agent, _, _ = make_agent(tmp_path / "d", [TurnResult("", [], StopKind.REFUSAL, detail="cyber")])
    result = await agent.run("x")
    assert result.status is TaskStatus.REFUSED and "cyber" in result.summary


async def test_truncated_tool_input_is_not_executed(tmp_path: Path) -> None:
    root = tmp_path / "d"
    agent, provider, _ = make_agent(
        root,
        [
            calls(
                ("write_file", {"path": "big.txt", "content": "partial"}), stop=StopKind.MAX_TOKENS
            ),
            text("ok"),
        ],
    )
    result = await agent.run("x")
    assert result.status is TaskStatus.COMPLETED
    assert not (root / "big.txt").exists()
    [outcome] = provider.conversation.tool_results()
    assert outcome.is_error and "length limit" in outcome.content


async def test_turn_limit(tmp_path: Path) -> None:
    script = [calls(("list_dir", {"path": "."})) for _ in range(3)]
    agent, _, _ = make_agent(tmp_path / "d", script, max_turns=3)
    result = await agent.run("loop forever")
    assert result.status is TaskStatus.LIMIT


async def test_retryable_llm_error_is_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    agent, _, _ = make_agent(tmp_path / "d", [LLMError("flaky", retryable=True), text("done")])
    assert (await agent.run("x")).status is TaskStatus.COMPLETED


async def test_fatal_llm_error_fails_task(tmp_path: Path) -> None:
    agent, _, _ = make_agent(tmp_path / "d", [LLMError("bad key")])
    result = await agent.run("x")
    assert result.status is TaskStatus.FAILED and "bad key" in result.summary


async def test_cancel_mid_tool_keeps_history_consistent(tmp_path: Path) -> None:
    root = tmp_path / "d"
    gate = asyncio.Event()

    class BlockingApprover(ScriptedApprover):
        async def request(self, req):  # type: ignore[no-untyped-def]
            gate.set()
            await asyncio.sleep(3600)
            raise AssertionError

    agent, provider, events = make_agent(
        root,
        [
            calls(
                ("write_file", {"path": "a.txt", "content": "a"}),
                ("run_shell", {"command": "echo hi"}),
                ("write_file", {"path": "c.txt", "content": "c"}),
            ),
            text("second goal done"),
        ],
        approver=BlockingApprover(),
    )
    task = asyncio.create_task(agent.run("do three things"))
    await gate.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events.events[-1]["status"] == "cancelled"
    results = provider.conversation.tool_results()
    # One result per tool_use, in order: the finished one kept, the rest closed out.
    assert [r.call_id for r in results] == ["c0-write_file", "c1-run_shell", "c2-write_file"]
    assert not results[0].is_error and results[1].is_error and results[2].is_error
    assert (root / "a.txt").exists() and not (root / "c.txt").exists()
    # The session is still usable.
    assert (await agent.run("next")).status is TaskStatus.COMPLETED


async def test_task_timeout(tmp_path: Path) -> None:
    async def slow() -> TurnResult:
        await asyncio.sleep(5)
        return text("late")

    agent, _, _ = make_agent(tmp_path / "d", [slow], task_timeout_s=0.1)
    result = await agent.run("x")
    assert result.status is TaskStatus.LIMIT


async def _no_sleep(_: float) -> None:
    return None
