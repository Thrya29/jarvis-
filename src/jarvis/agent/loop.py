"""The agent loop: model decides -> tools act (through the safety layer) -> results feed back.

An :class:`Agent` is a session: successive goals share one conversation so follow-ups
("now email that to Priya") have context. The loop is cancellable at any await point,
which is what voice barge-in and the kill switch rely on.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from enum import StrEnum

from jarvis.agent.events import EventSink, null_sink
from jarvis.agent.prompts import build_system_prompt, goal_message
from jarvis.core.config import AgentConfig
from jarvis.llm.base import (
    Conversation,
    LLMError,
    LLMProvider,
    StopKind,
    ToolCall,
    ToolOutcome,
    TurnResult,
    Usage,
)
from jarvis.tools.base import ToolContext, ToolRegistry

log = logging.getLogger(__name__)

LLM_RETRIES = 2
COMPUTER = "computer"
HALT_TEXT = "Not executed: an earlier computer action in this turn failed."


class TaskStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REFUSED = "refused"
    LIMIT = "limit_reached"


@dataclass
class _Inflight:
    """Tool calls from the current model turn that still need results."""

    calls: list[ToolCall] = field(default_factory=list)
    outcomes: list[ToolOutcome] = field(default_factory=list)


@dataclass
class TaskResult:
    task_id: str
    status: TaskStatus
    summary: str
    usage: Usage = field(default_factory=Usage)
    tool_calls: int = 0


class Agent:
    def __init__(
        self,
        provider: LLMProvider,
        registry: ToolRegistry,
        ctx: ToolContext,
        cfg: AgentConfig,
        emit: EventSink = null_sink,
    ) -> None:
        self._registry = registry
        self._ctx = ctx
        self._cfg = cfg
        self._emit = emit
        ctx.emit = emit
        screen = ctx.desktop is not None
        self._conv: Conversation = provider.new_conversation(
            build_system_prompt(
                ctx.guard.allowed,
                screen=screen,
                pixel_control=screen and provider.supports_computer_use,
                kill_hotkey=ctx.settings.safety.kill_hotkey,
            ),
            registry.specs(),
            computer_use=screen and provider.supports_computer_use,
        )
        self._lock = asyncio.Lock()

    def close(self) -> None:
        """Release OS resources (UI Automation thread, held keys)."""
        if self._ctx.desktop is not None:
            self._ctx.desktop.close()

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    async def run(self, goal: str, voice: bool = False) -> TaskResult:
        async with self._lock:
            task_id = uuid.uuid4().hex[:12]
            self._ctx.task_id = task_id
            result = TaskResult(task_id, TaskStatus.FAILED, "")
            self._ctx.audit.record("task.start", task_id=task_id, goal=goal)
            await self._emit({"type": "task.started", "task_id": task_id, "goal": goal})
            pending = _Inflight()
            try:
                async with asyncio.timeout(self._cfg.task_timeout_s):
                    await self._loop(goal, result, pending, voice)
            except TimeoutError:
                result.status = TaskStatus.LIMIT
                result.summary = f"Stopped: the task exceeded {self._cfg.task_timeout_s:.0f}s."
                self._close_pending(pending, "Task timed out before this ran.")
            except asyncio.CancelledError:
                result.status = TaskStatus.CANCELLED
                result.summary = "Cancelled by the user."
                self._close_pending(pending, "Cancelled by the user before this finished.")
                await self._finish(result)
                raise
            except LLMError as exc:
                result.status = TaskStatus.FAILED
                result.summary = f"Stopped: {exc}"
                self._close_pending(pending, "Not run: the task failed.")
            await self._finish(result)
            return result

    async def _finish(self, result: TaskResult) -> None:
        if self._ctx.desktop is not None:
            # Screen consent is per task; also let go of any keys/buttons still held.
            self._ctx.desktop.end_task(result.task_id)
        self._ctx.audit.record(
            "task.finish",
            task_id=result.task_id,
            status=result.status.value,
            tool_calls=result.tool_calls,
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
        )
        await asyncio.shield(
            self._emit(
                {
                    "type": "task.finished",
                    "task_id": result.task_id,
                    "status": result.status.value,
                    "summary": result.summary,
                    "tool_calls": result.tool_calls,
                    "usage": {
                        "input_tokens": result.usage.input_tokens,
                        "output_tokens": result.usage.output_tokens,
                        "cache_read_tokens": result.usage.cache_read_tokens,
                    },
                }
            )
        )

    def _close_pending(self, pending: _Inflight, reason: str) -> None:
        # Every tool_use needs a tool_result (all in one message) before the next user
        # turn, or the conversation is unusable for follow-up goals.
        if not pending.calls:
            return
        done = {o.call_id for o in pending.outcomes}
        results = list(pending.outcomes) + [
            ToolOutcome(c.id, c.name, reason, is_error=True, toolset=c.toolset)
            for c in pending.calls
            if c.id not in done
        ]
        self._conv.add_tool_results(results)
        pending.calls.clear()
        pending.outcomes.clear()

    async def _step(self) -> TurnResult:
        for attempt in range(LLM_RETRIES + 1):
            try:
                return await self._conv.step()
            except LLMError as exc:
                if not exc.retryable or attempt == LLM_RETRIES:
                    raise
                log.warning("LLM call failed (%s); retrying", exc)
                await asyncio.sleep(2**attempt)
        raise AssertionError("unreachable")

    async def _loop(
        self, goal: str, result: TaskResult, pending: _Inflight, voice: bool = False
    ) -> None:
        self._conv.add_user(goal_message(goal, voice=voice))
        for _ in range(self._cfg.max_turns):
            turn = await self._step()
            result.usage.add(turn.usage)
            if turn.text.strip():
                await self._emit(
                    {"type": "assistant.text", "task_id": result.task_id, "text": turn.text}
                )

            if turn.stop is StopKind.REFUSAL:
                result.status = TaskStatus.REFUSED
                result.summary = "The model declined this request" + (
                    f" ({turn.detail})." if turn.detail else "."
                )
                return

            if turn.stop is StopKind.PAUSE and not turn.tool_calls:
                continue

            if turn.tool_calls:
                pending.calls = list(turn.tool_calls)
                pending.outcomes = []
                if turn.stop is StopKind.MAX_TOKENS:
                    # Output was cut off mid tool input: never run a truncated call.
                    self._close_pending(
                        pending,
                        "Your output hit the length limit and this tool input was cut off. "
                        "Retry with smaller steps (e.g. write large content in parts).",
                    )
                    continue
                halted = False
                for call in turn.tool_calls:
                    if halted and call.toolset == COMPUTER:
                        # Batch rule: after a failed computer action, later ones are skipped.
                        pending.outcomes.append(
                            ToolOutcome(call.id, call.name, HALT_TEXT, True, toolset=COMPUTER)
                        )
                        continue
                    outcome = await self._run_tool(call, result)
                    halted = halted or (outcome.is_error and call.toolset == COMPUTER)
                    pending.outcomes.append(outcome)
                self._conv.add_tool_results(pending.outcomes)
                pending.calls, pending.outcomes = [], []
                continue

            result.status = TaskStatus.COMPLETED
            result.summary = turn.text.strip() or "Done."
            if turn.stop is StopKind.MAX_TOKENS:
                result.summary += "\n[response was cut off at the length limit]"
            return

        result.status = TaskStatus.LIMIT
        result.summary = f"Stopped after {self._cfg.max_turns} steps without finishing."

    async def _run_tool(self, call: ToolCall, result: TaskResult) -> ToolOutcome:
        result.tool_calls += 1
        await self._emit(
            {
                "type": "tool.started",
                "task_id": result.task_id,
                "call_id": call.id,
                "tool": call.name,
            }
        )
        outcome = await self._registry.execute(call, self._ctx)
        await self._emit(
            {
                "type": "tool.finished",
                "task_id": result.task_id,
                "call_id": call.id,
                "tool": call.name,
                "ok": not outcome.is_error,
                "preview": outcome.content[:300],
            }
        )
        return outcome
