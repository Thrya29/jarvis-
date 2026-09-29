from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from jarvis.agent.events import EventSink
from jarvis.agent.factory import all_tools
from jarvis.agent.loop import Agent
from jarvis.core.config import AgentConfig, Settings
from jarvis.safety.policy import Approver
from jarvis.server.app import create_app
from jarvis.tools.base import ToolRegistry
from tests.fakes import ScriptedProvider, calls, make_ctx, text

TOKEN = "k" * 43


def _client(tmp_path: Path, script: list[Any]) -> TestClient:
    def factory(approver: Approver, emit: EventSink) -> Agent:
        return Agent(
            ScriptedProvider(script),
            ToolRegistry(all_tools()),
            make_ctx(tmp_path / "docs", approver),
            AgentConfig(),
            emit,
        )

    return TestClient(create_app(Settings(), TOKEN, factory), base_url="http://127.0.0.1")


def _collect_until(ws: Any, kind: str) -> list[dict[str, Any]]:
    seen = []
    while True:
        msg = ws.receive_json()
        seen.append(msg)
        if msg["type"] == kind:
            return seen


def test_task_with_approval_over_websocket(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "old.txt").write_text("x", encoding="utf-8")
    client = _client(
        tmp_path,
        [
            calls(("write_file", {"path": "old.txt", "content": "new", "overwrite": True})),
            text("Replaced."),
        ],
    )
    with client.websocket_connect("ws://127.0.0.1/v1/ws") as ws:
        ws.send_json({"type": "auth", "token": TOKEN})
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "task.start", "goal": "replace old.txt"})
        before = _collect_until(ws, "approval.request")
        assert before[0]["type"] == "task.started"
        req = before[-1]
        assert req["risk"] == "destructive" and "old.txt" in req["summary"]
        ws.send_json({"type": "approval.response", "id": req["id"], "approved": True})
        after = _collect_until(ws, "task.finished")
        assert after[-1]["status"] == "completed" and after[-1]["summary"] == "Replaced."
    assert (tmp_path / "docs" / "old.txt").read_text(encoding="utf-8") == "new"


def test_declined_over_websocket(tmp_path: Path) -> None:
    client = _client(tmp_path, [calls(("run_shell", {"command": "echo hi"})), text("Understood.")])
    with client.websocket_connect("ws://127.0.0.1/v1/ws") as ws:
        ws.send_json({"type": "auth", "token": TOKEN})
        ws.receive_json()
        ws.send_json({"type": "task.start", "goal": "run something"})
        req = _collect_until(ws, "approval.request")[-1]
        ws.send_json({"type": "approval.response", "id": req["id"], "approved": False})
        events = _collect_until(ws, "task.finished")
        finished = [e for e in events if e["type"] == "tool.finished"]
        assert finished and not finished[0]["ok"]


def test_errors_are_reported(tmp_path: Path) -> None:
    client = TestClient(create_app(Settings(), TOKEN, None), base_url="http://127.0.0.1")
    with client.websocket_connect("ws://127.0.0.1/v1/ws") as ws:
        ws.send_json({"type": "auth", "token": TOKEN})
        ws.receive_json()
        ws.send_json({"type": "task.start", "goal": "x"})
        assert "agent unavailable" in ws.receive_json()["error"]
        ws.send_json({"type": "bogus"})
        assert ws.receive_json()["type"] == "error"
