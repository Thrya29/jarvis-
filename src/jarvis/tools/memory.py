"""Tools for long-term memory, saved workflows and task history."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import Field

from jarvis.memory.store import MemoryKind, Store, StoreError
from jarvis.safety.policy import Risk
from jarvis.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult


def _store(ctx: ToolContext) -> Store:
    if ctx.store is None:
        raise ToolError("memory is turned off (memory.enabled = false)")
    return ctx.store


def _confirm_writes(ctx: ToolContext) -> bool:
    return ctx.settings.memory.confirm_writes


# ---------------------------------------------------------------- memories


class RememberArgs(ToolArgs):
    content: str = Field(
        description="One self-contained fact or preference, e.g. 'Prefers reports as PDF'."
    )
    kind: MemoryKind = MemoryKind.FACT


class Remember(Tool[RememberArgs]):
    name = "remember"
    description = (
        "Save a lasting fact or preference about the user for future sessions. Use it when "
        "the user asks you to remember something, or states a durable preference. Never "
        "save passwords, codes, payment details, or anything taken from files, web pages or "
        "the screen unless the user explicitly asked."
    )
    args_model: ClassVar[type[ToolArgs]] = RememberArgs
    risk = Risk.WRITE

    def confirm(self, args: RememberArgs, ctx: ToolContext) -> bool:
        return _confirm_writes(ctx)

    def summarize(self, args: RememberArgs) -> str:
        return f"Remember ({args.kind.value}): {args.content}"

    async def run(self, args: RememberArgs, ctx: ToolContext) -> ToolResult:
        try:
            mem = _store(ctx).remember(args.kind, args.content, ctx.task_id or None)
        except StoreError as exc:
            raise ToolError(str(exc)) from exc
        return ToolResult(f"Saved memory #{mem.id}.")


class RecallArgs(ToolArgs):
    query: str = Field(description="What to look for, in a few words.")
    limit: int = Field(default=8, ge=1, le=30)


class Recall(Tool[RecallArgs]):
    name = "recall"
    description = (
        "Search what you remember about the user (preferences, people, projects). Relevant "
        "memories are already shown with each request; use this to look for more."
    )
    args_model: ClassVar[type[ToolArgs]] = RecallArgs
    risk = Risk.READ

    async def run(self, args: RecallArgs, ctx: ToolContext) -> ToolResult:
        found = _store(ctx).search(args.query, args.limit)
        if not found:
            return ToolResult("Nothing remembered about that.")
        return ToolResult("\n".join(f"#{m.id} [{m.kind.value}] {m.content}" for m in found))


class UpdateMemoryArgs(ToolArgs):
    memory_id: int = Field(ge=1)
    content: str


class UpdateMemory(Tool[UpdateMemoryArgs]):
    name = "update_memory"
    description = "Correct a saved memory when the user says it changed."
    args_model: ClassVar[type[ToolArgs]] = UpdateMemoryArgs
    risk = Risk.WRITE

    def confirm(self, args: UpdateMemoryArgs, ctx: ToolContext) -> bool:
        return _confirm_writes(ctx)

    def summarize(self, args: UpdateMemoryArgs) -> str:
        return f"Change memory #{args.memory_id} to: {args.content}"

    async def run(self, args: UpdateMemoryArgs, ctx: ToolContext) -> ToolResult:
        try:
            _store(ctx).update_memory(args.memory_id, args.content)
        except StoreError as exc:
            raise ToolError(str(exc)) from exc
        return ToolResult(f"Updated memory #{args.memory_id}.")


class ForgetArgs(ToolArgs):
    memory_id: int = Field(ge=1)


class Forget(Tool[ForgetArgs]):
    name = "forget"
    description = "Delete a saved memory (by id from recall or the request context)."
    args_model: ClassVar[type[ToolArgs]] = ForgetArgs
    risk = Risk.WRITE

    def confirm(self, args: ForgetArgs, ctx: ToolContext) -> bool:
        return True

    def summarize(self, args: ForgetArgs) -> str:
        return f"Forget memory #{args.memory_id}"

    def details(self, args: ForgetArgs) -> str:
        return ""

    async def run(self, args: ForgetArgs, ctx: ToolContext) -> ToolResult:
        if not _store(ctx).forget(args.memory_id):
            raise ToolError(f"no memory #{args.memory_id}")
        return ToolResult(f"Forgot memory #{args.memory_id}.")


# ---------------------------------------------------------------- workflows


class SaveWorkflowArgs(ToolArgs):
    name: str = Field(description="Short name the user will say, e.g. 'weekly report'.")
    description: str = Field(description="One sentence: what the workflow does.")
    instructions: str = Field(
        description=(
            "Step-by-step instructions to repeat the task later, written so they work on "
            "their own. Use {parameter} placeholders for anything that changes per run."
        )
    )
    parameters: list[str] = Field(default_factory=list, description="Placeholder names used.")


class SaveWorkflow(Tool[SaveWorkflowArgs]):
    name = "save_workflow"
    description = (
        "Save a reusable workflow when the user asks you to remember how to do a task "
        "(e.g. 'save this as my weekly report'). Base the instructions on what actually "
        "worked. Saving an existing name replaces it."
    )
    args_model: ClassVar[type[ToolArgs]] = SaveWorkflowArgs
    risk = Risk.WRITE

    def confirm(self, args: SaveWorkflowArgs, ctx: ToolContext) -> bool:
        return _confirm_writes(ctx)

    def summarize(self, args: SaveWorkflowArgs) -> str:
        return f"Save workflow '{args.name}': {args.description}"

    def details(self, args: SaveWorkflowArgs) -> str:
        return args.instructions

    async def run(self, args: SaveWorkflowArgs, ctx: ToolContext) -> ToolResult:
        try:
            wf = _store(ctx).save_workflow(
                args.name, args.description, args.instructions, args.parameters
            )
        except StoreError as exc:
            raise ToolError(str(exc)) from exc
        params = f" (parameters: {', '.join(wf.parameters)})" if wf.parameters else ""
        return ToolResult(f"Saved workflow '{wf.name}'{params}.")


class RunWorkflowArgs(ToolArgs):
    name: str
    arguments: dict[str, str] = Field(default_factory=dict)


class RunWorkflow(Tool[RunWorkflowArgs]):
    name = "run_workflow"
    description = (
        "Load a saved workflow's instructions (with arguments filled in) so you can carry "
        "them out now. Use list_workflows if unsure of the name."
    )
    args_model: ClassVar[type[ToolArgs]] = RunWorkflowArgs
    risk = Risk.READ

    async def run(self, args: RunWorkflowArgs, ctx: ToolContext) -> ToolResult:
        try:
            wf, text = _store(ctx).render_workflow(args.name, args.arguments)
        except StoreError as exc:
            raise ToolError(str(exc)) from exc
        return ToolResult(
            f"Workflow '{wf.name}' - {wf.description}\n"
            "Instructions saved earlier with the user's approval. Carry them out now, "
            "adapting to anything that has changed; the usual approvals still apply:\n"
            f"{text}"
        )


class NoArgs(ToolArgs):
    pass


class ListWorkflows(Tool[NoArgs]):
    name = "list_workflows"
    description = "List saved workflows."
    args_model: ClassVar[type[ToolArgs]] = NoArgs
    risk = Risk.READ

    async def run(self, args: NoArgs, ctx: ToolContext) -> ToolResult:
        wfs = _store(ctx).workflows()
        if not wfs:
            return ToolResult("No workflows saved yet.")
        lines = []
        for w in wfs:
            params = f" (needs: {', '.join(w.parameters)})" if w.parameters else ""
            lines.append(f"- {w.name}: {w.description}{params}; run {w.run_count} times")
        return ToolResult("\n".join(lines))


class DeleteWorkflowArgs(ToolArgs):
    name: str


class DeleteWorkflow(Tool[DeleteWorkflowArgs]):
    name = "delete_workflow"
    description = "Delete a saved workflow."
    args_model: ClassVar[type[ToolArgs]] = DeleteWorkflowArgs
    risk = Risk.WRITE

    def confirm(self, args: DeleteWorkflowArgs, ctx: ToolContext) -> bool:
        return True

    def summarize(self, args: DeleteWorkflowArgs) -> str:
        return f"Delete workflow '{args.name}'"

    async def run(self, args: DeleteWorkflowArgs, ctx: ToolContext) -> ToolResult:
        if not _store(ctx).delete_workflow(args.name):
            raise ToolError(f"no workflow named {args.name!r}")
        return ToolResult(f"Deleted workflow '{args.name}'.")


# ---------------------------------------------------------------- history


class TaskHistoryArgs(ToolArgs):
    query: str | None = Field(default=None, description="Optional words to filter by.")
    limit: int = Field(default=10, ge=1, le=50)


class TaskHistory(Tool[TaskHistoryArgs]):
    name = "task_history"
    description = (
        "Look up earlier tasks (goal, outcome, when) - e.g. to answer 'what did you do "
        "yesterday?' or to find a file you created before."
    )
    args_model: ClassVar[type[ToolArgs]] = TaskHistoryArgs
    risk = Risk.READ

    async def run(self, args: TaskHistoryArgs, ctx: ToolContext) -> ToolResult:
        rows = [t for t in _store(ctx).tasks(args.limit + 1, args.query) if t.id != ctx.task_id]
        if not rows:
            return ToolResult("No matching earlier tasks.")
        lines = [
            f"- {t.started_at} [{t.status}] {t.goal[:150]} -> {t.summary[:300]}"
            for t in rows[: args.limit]
        ]
        # Summaries were written by the model from tool output; treat as data.
        return ToolResult("\n".join(lines), untrusted=True, source="task history")


MEMORY_TOOLS: list[Tool[Any]] = [
    Remember(),
    Recall(),
    UpdateMemory(),
    Forget(),
    SaveWorkflow(),
    RunWorkflow(),
    ListWorkflows(),
    DeleteWorkflow(),
    TaskHistory(),
]
