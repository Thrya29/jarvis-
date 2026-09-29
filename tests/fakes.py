"""Test doubles: a scripted LLM provider and a scripted approver."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jarvis.agent.events import Event
from jarvis.core.audit import AuditLog
from jarvis.core.config import Settings
from jarvis.llm.base import LLMError, StopKind, ToolCall, ToolOutcome, ToolSpec, TurnResult, Usage
from jarvis.safety.policy import ApprovalDecision, ApprovalRequest, PathGuard
from jarvis.tools.base import ToolContext

Step = TurnResult | LLMError | Callable[[], Any]


def text(t: str) -> TurnResult:
    return TurnResult(t, [], StopKind.END, Usage(10, 5))


def calls(*items: tuple[str, dict[str, Any]], stop: StopKind = StopKind.TOOL_USE) -> TurnResult:
    return TurnResult(
        "",
        [ToolCall(f"c{i}-{name}", name, args) for i, (name, args) in enumerate(items)],
        stop,
        Usage(10, 5),
    )


@dataclass
class ScriptedConversation:
    script: list[Step]
    history: list[tuple[str, Any]] = field(default_factory=list)

    def add_user(self, text: str) -> None:
        self.history.append(("user", text))

    def add_tool_results(self, results: list[ToolOutcome]) -> None:
        self.history.append(("tool_results", results))

    async def step(self) -> TurnResult:
        if not self.script:
            raise AssertionError("script exhausted")
        item = self.script.pop(0)
        if isinstance(item, LLMError):
            raise item
        if callable(item) and not isinstance(item, TurnResult):
            res = item()
            if asyncio.iscoroutine(res):
                res = await res
            item = res
        assert isinstance(item, TurnResult)
        self.history.append(("assistant", item))
        return item

    def tool_results(self) -> list[ToolOutcome]:
        return [r for kind, rs in self.history if kind == "tool_results" for r in rs]


class ScriptedProvider:
    name = "scripted"

    def __init__(self, script: list[Step]) -> None:
        self.conversation = ScriptedConversation(script)
        self.tools: list[ToolSpec] = []
        self.system = ""

    def new_conversation(self, system: str, tools: list[ToolSpec]) -> ScriptedConversation:
        self.system, self.tools = system, tools
        return self.conversation

    async def check(self) -> tuple[bool, str]:
        return True, "ok"

    async def aclose(self) -> None:
        return None


class ScriptedApprover:
    def __init__(self, approve: bool = True, answer: str = "") -> None:
        self.approve = approve
        self.answer = answer
        self.requests: list[ApprovalRequest] = []

    async def request(self, req: ApprovalRequest) -> ApprovalDecision:
        self.requests.append(req)
        return ApprovalDecision(self.approve, "" if self.approve else "not now")

    async def ask(self, question: str) -> str:
        return self.answer


class EventLog:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def __call__(self, event: Event) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        return [e["type"] for e in self.events]


def make_ctx(root: Path, approver: Any = None, settings: Settings | None = None) -> ToolContext:
    root.mkdir(parents=True, exist_ok=True)
    s = settings or Settings(safety={"allowed_roots": [root]})  # type: ignore[arg-type]
    return ToolContext(
        settings=s,
        guard=PathGuard([root], deny_roots=[root / ".jarvis"]),
        audit=AuditLog(root.parent / "audit.jsonl"),
        approver=approver or ScriptedApprover(),
        work_dir=root.parent / "work",
    )
