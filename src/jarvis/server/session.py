"""One WebSocket client = one agent session (its own conversation).

Only one task may run on the machine at a time, across all clients: two agents
driving the same desktop would fight over it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any

from fastapi import WebSocket

from jarvis.agent.events import Event, EventSink
from jarvis.agent.loop import Agent
from jarvis.safety.policy import ApprovalDecision, ApprovalRequest, Approver

log = logging.getLogger(__name__)

APPROVAL_TIMEOUT_S = 300.0

AgentFactory = Callable[[Approver, EventSink], Agent]


class WebSocketApprover:
    def __init__(self, send: EventSink) -> None:
        self._send = send
        self._waiting: dict[str, asyncio.Future[Any]] = {}

    async def _round_trip(self, message: Event, key: str) -> Any:
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._waiting[key] = fut
        try:
            await self._send(message)
            return await asyncio.wait_for(fut, APPROVAL_TIMEOUT_S)
        finally:
            self._waiting.pop(key, None)

    async def request(self, req: ApprovalRequest) -> ApprovalDecision:
        try:
            reply = await self._round_trip(
                {
                    "type": "approval.request",
                    "id": req.id,
                    "tool": req.tool,
                    "summary": req.summary,
                    "risk": req.risk.name.lower(),
                    "details": req.details,
                },
                req.id,
            )
        except TimeoutError:
            return ApprovalDecision(False, "no response from the user")
        return ApprovalDecision(bool(reply.get("approved")), str(reply.get("note") or ""))

    async def ask(self, question: str) -> str:
        key = f"ask-{id(question)}-{asyncio.get_running_loop().time()}"
        try:
            reply = await self._round_trip(
                {"type": "ask.request", "id": key, "question": question}, key
            )
        except TimeoutError:
            return ""
        return str(reply.get("answer") or "")

    def resolve(self, key: str, payload: dict[str, Any]) -> bool:
        fut = self._waiting.get(key)
        if fut is None or fut.done():
            return False
        fut.set_result(payload)
        return True

    def cancel_all(self) -> None:
        for fut in self._waiting.values():
            if not fut.done():
                fut.set_result({"approved": False, "note": "client disconnected"})


class ClientSession:
    def __init__(self, ws: WebSocket, factory: AgentFactory | None, machine_lock: asyncio.Lock):
        self._ws = ws
        self._send_lock = asyncio.Lock()
        self._machine_lock = machine_lock
        self.approver = WebSocketApprover(self.send)
        self._agent = factory(self.approver, self.send) if factory else None
        self._task: asyncio.Task[None] | None = None

    async def send(self, message: Event) -> None:
        async with self._send_lock:
            with contextlib.suppress(RuntimeError):  # socket already closed
                await self._ws.send_json(message)

    async def handle(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        if kind == "ping":
            await self.send({"type": "pong"})
        elif kind == "task.start":
            await self._start(str(msg.get("goal") or "").strip())
        elif kind == "task.cancel":
            if self._task and not self._task.done():
                self._task.cancel()
        elif kind == "approval.response" or kind == "ask.response":
            self.approver.resolve(str(msg.get("id")), msg)
        else:
            await self.send({"type": "error", "error": f"unsupported message type {kind!r}"})

    async def _start(self, goal: str) -> None:
        if self._agent is None:
            await self.send({"type": "error", "error": "agent unavailable - run `jarvis doctor`"})
        elif not goal:
            await self.send({"type": "error", "error": "goal is empty"})
        elif self._machine_lock.locked():
            await self.send({"type": "error", "error": "another task is already running"})
        else:
            agent = self._agent

            async def run() -> None:
                async with self._machine_lock:
                    await agent.run(goal)

            self._task = asyncio.create_task(run())

    async def close(self) -> None:
        self.approver.cancel_all()
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
