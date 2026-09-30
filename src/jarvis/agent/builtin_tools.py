"""Tools that talk to the user rather than the machine."""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import Field

from jarvis.safety.policy import Risk
from jarvis.tools.base import Tool, ToolArgs, ToolContext, ToolResult


class PlanStep(ToolArgs):
    title: str
    status: Literal["pending", "in_progress", "done", "failed", "skipped"] = "pending"


class UpdatePlanArgs(ToolArgs):
    steps: list[PlanStep] = Field(min_length=1, max_length=30)


class UpdatePlan(Tool[UpdatePlanArgs]):
    name = "update_plan"
    description = (
        "Publish or update your step-by-step plan for the current goal so the user can "
        "follow progress. Call it before starting multi-step work and whenever a step's "
        "status changes or the plan changes."
    )
    args_model: ClassVar[type[ToolArgs]] = UpdatePlanArgs
    risk = Risk.READ

    def summarize(self, args: UpdatePlanArgs) -> str:
        done = sum(s.status == "done" for s in args.steps)
        return f"Plan: {done}/{len(args.steps)} steps done"

    async def run(self, args: UpdatePlanArgs, ctx: ToolContext) -> ToolResult:
        await ctx.emit({"type": "plan.updated", "steps": [s.model_dump() for s in args.steps]})
        return ToolResult("Plan updated.")


class AskUserArgs(ToolArgs):
    question: str


class AskUser(Tool[AskUserArgs]):
    name = "ask_user"
    description = (
        "Ask the user a short clarifying question when the goal is ambiguous and a wrong "
        "guess would waste work or cause harm. Don't use it for routine confirmation - "
        "risky actions are confirmed automatically."
    )
    args_model: ClassVar[type[ToolArgs]] = AskUserArgs
    risk = Risk.READ

    def summarize(self, args: AskUserArgs) -> str:
        return f"Ask: {args.question}"

    async def run(self, args: AskUserArgs, ctx: ToolContext) -> ToolResult:
        answer = await ctx.approver.ask(args.question)
        if not answer.strip():
            return ToolResult("The user did not answer. Proceed with your best judgement or stop.")
        return ToolResult(f"User answered: {answer}")


BUILTIN_TOOLS: list[Tool[Any]] = [UpdatePlan(), AskUser()]
