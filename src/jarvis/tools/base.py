"""Tool contract and the registry that executes tool calls safely.

Every call passes through the same pipeline:
validate input -> classify risk -> ask the user if required -> audit -> run with
a timeout -> wrap untrusted output -> truncate -> audit result.
"""

from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, ValidationError

from jarvis.agent.events import EventSink, null_sink
from jarvis.core.audit import AuditLog
from jarvis.core.config import Settings
from jarvis.llm.base import ToolCall, ToolOutcome, ToolSpec
from jarvis.safety.policy import (
    ApprovalRequest,
    Approver,
    PathDeniedError,
    PathGuard,
    Risk,
    needs_approval,
)

log = logging.getLogger(__name__)


class ToolError(Exception):
    """An expected failure reported back to the model (not a bug)."""


class ToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass
class ToolResult:
    content: str
    # Content that came from outside JARVIS (files, web pages, command output).
    # It is fenced so the model treats it as data, never as instructions.
    untrusted: bool = False
    source: str = ""


@dataclass
class ToolContext:
    settings: Settings
    guard: PathGuard
    audit: AuditLog
    approver: Approver
    work_dir: Path  # scratch space for sandboxed code
    emit: EventSink = null_sink


class Tool[A: ToolArgs](ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    args_model: ClassVar[type[ToolArgs]]
    risk: ClassVar[Risk]

    def risk_for(self, args: A, ctx: ToolContext) -> Risk:
        """Risk can depend on arguments (e.g. overwriting vs creating)."""
        return self.risk

    def summarize(self, args: A) -> str:
        """One-line human description used in approval prompts and the activity feed."""
        fields = ", ".join(f"{k}={v!r}" for k, v in args.model_dump().items())
        return f"{self.name}({fields[:200]})"

    def details(self, args: A) -> str:
        return ""

    @abstractmethod
    async def run(self, args: A, ctx: ToolContext) -> ToolResult: ...

    def spec(self) -> ToolSpec:
        schema = self.args_model.model_json_schema()
        schema.pop("title", None)
        return ToolSpec(self.name, self.description, schema)


def fence_untrusted(content: str, source: str) -> str:
    safe_source = source.replace('"', "'")
    body = content.replace("</untrusted_content>", "</untrusted_content_>")
    return f'<untrusted_content source="{safe_source}">\n{body}\n</untrusted_content>'


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit * 3 // 4]
    tail = text[-limit // 4 :]
    return f"{head}\n\n[... {len(text) - limit} characters omitted ...]\n\n{tail}"


class ToolRegistry:
    def __init__(self, tools: list[Tool[Any]]) -> None:
        self._tools = {t.name: t for t in tools}
        if len(self._tools) != len(tools):
            raise ValueError("duplicate tool names")

    def specs(self) -> list[ToolSpec]:
        return [t.spec() for t in self._tools.values()]

    def names(self) -> list[str]:
        return list(self._tools)

    async def execute(self, call: ToolCall, ctx: ToolContext) -> ToolOutcome:
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolOutcome(call.id, call.name, f"Unknown tool {call.name!r}.", is_error=True)

        try:
            args = tool.args_model.model_validate(call.input)
        except ValidationError as exc:
            # Includes truncated/garbled inputs from eager input streaming.
            payload = json.dumps(
                {"INVALID_INPUT": call.input, "errors": exc.errors(include_url=False)},
                default=str,
            )
            return ToolOutcome(call.id, call.name, payload, is_error=True)

        try:
            risk = tool.risk_for(args, ctx)
        except (PathDeniedError, ToolError) as exc:
            ctx.audit.record("tool.denied", tool=call.name, reason=str(exc))
            return ToolOutcome(call.id, call.name, str(exc), is_error=True)

        summary = tool.summarize(args)
        if needs_approval(risk, ctx.settings.safety):
            decision = await ctx.approver.request(
                ApprovalRequest(tool.name, summary, risk, tool.details(args))
            )
            ctx.audit.record(
                "tool.approval",
                tool=call.name,
                risk=risk.name,
                summary=summary,
                approved=decision.approved,
                note=decision.note,
            )
            if not decision.approved:
                msg = "The user declined this action."
                if decision.note:
                    msg += f" Their note: {decision.note}"
                return ToolOutcome(call.id, call.name, msg, is_error=True)

        ctx.audit.record("tool.call", tool=call.name, risk=risk.name, summary=summary)
        try:
            result = await asyncio.wait_for(
                tool.run(args, ctx), timeout=ctx.settings.agent.tool_timeout_s
            )
        except (ToolError, PathDeniedError, FileNotFoundError, FileExistsError) as exc:
            ctx.audit.record("tool.error", tool=call.name, error=str(exc))
            return ToolOutcome(call.id, call.name, str(exc), is_error=True)
        except TimeoutError:
            ctx.audit.record("tool.error", tool=call.name, error="timeout")
            return ToolOutcome(
                call.id,
                call.name,
                f"Timed out after {ctx.settings.agent.tool_timeout_s:.0f}s.",
                is_error=True,
            )
        except OSError as exc:
            ctx.audit.record("tool.error", tool=call.name, error=str(exc))
            return ToolOutcome(call.id, call.name, f"OS error: {exc}", is_error=True)
        except Exception as exc:
            # A bug in a tool must not take down the task; surface it and keep going.
            log.exception("tool %s crashed", call.name)
            ctx.audit.record("tool.crash", tool=call.name, error=repr(exc))
            return ToolOutcome(
                call.id, call.name, f"Internal error in {call.name}: {exc!r}", is_error=True
            )

        content = truncate(result.content, ctx.settings.agent.max_tool_output_chars)
        if result.untrusted:
            content = fence_untrusted(content, result.source or call.name)
        ctx.audit.record("tool.done", tool=call.name, chars=len(result.content))
        return ToolOutcome(call.id, call.name, content)
