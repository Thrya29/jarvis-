"""Terminal front-end: prints agent events and asks for approvals on stdin."""

from __future__ import annotations

import asyncio
import sys
import textwrap

from jarvis.agent.events import Event
from jarvis.safety.policy import ApprovalDecision, ApprovalRequest, Risk

_RISK_LABEL = {
    Risk.READ: "read",
    Risk.WRITE: "write",
    Risk.EXECUTE: "run code / network",
    Risk.DESTRUCTIVE: "DESTRUCTIVE",
    Risk.EXTERNAL: "EXTERNAL",
}


def _out(text: str = "") -> None:
    print(text, flush=True)


async def _input(prompt: str) -> str:
    try:
        return await asyncio.to_thread(input, prompt)
    except EOFError:
        return ""


class ConsoleApprover:
    def __init__(self, assume_no: bool = False) -> None:
        self._assume_no = assume_no or not sys.stdin.isatty()
        self._lock = asyncio.Lock()

    async def request(self, req: ApprovalRequest) -> ApprovalDecision:
        async with self._lock:
            _out(f"\n  ⚠  Approval needed [{_RISK_LABEL[req.risk]}]: {req.summary}")
            if req.details:
                _out(textwrap.indent(req.details[:1500], "     │ "))
            if self._assume_no:
                _out("     → declined (non-interactive)")
                return ApprovalDecision(False, "non-interactive session")
            answer = (await _input("     Allow? [y/N/or type a note to decline] ")).strip()
            if answer.lower() in {"y", "yes"}:
                return ApprovalDecision(True)
            note = "" if answer.lower() in {"", "n", "no"} else answer
            return ApprovalDecision(False, note)

    async def ask(self, question: str) -> str:
        async with self._lock:
            _out(f"\n  ?  {question}")
            if self._assume_no:
                return ""
            return (await _input("     > ")).strip()


async def print_event(event: Event) -> None:
    kind = event["type"]
    if kind == "plan.updated":
        marks = {"done": "✔", "in_progress": "▶", "failed": "✖", "skipped": "-", "pending": "·"}
        _out("\n  Plan:")
        for step in event["steps"]:
            _out(f"   {marks.get(step['status'], '·')} {step['title']}")
    elif kind == "assistant.text":
        _out("\n" + textwrap.indent(event["text"].strip(), "  "))
    elif kind == "tool.started":
        _out(f"  … {event['tool']}")
    elif kind == "tool.finished" and not event["ok"]:
        _out(f"    ✖ {event['tool']}: {event['preview'][:200]}")
    elif kind == "task.finished":
        usage = event["usage"]
        _out(
            f"\n  [{event['status']}] {event['tool_calls']} tool calls, "
            f"{usage['input_tokens']:,} in / {usage['output_tokens']:,} out tokens"
        )
