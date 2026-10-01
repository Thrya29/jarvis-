"""Ask my documents: search the user's indexed files."""

from __future__ import annotations

import asyncio
import time
from typing import Any, ClassVar

from pydantic import Field

from jarvis.safety.policy import Risk
from jarvis.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult

REFRESH_AFTER_S = 30 * 60  # re-scan folders for changes at most every 30 minutes
SEARCH_SYNC_BUDGET_S = 45.0  # how long one search may spend catching up on indexing


class SearchDocumentsArgs(ToolArgs):
    query: str = Field(description="What to look for, as a question or keywords.")
    limit: int = Field(default=6, ge=1, le=20)


class SearchDocuments(Tool[SearchDocumentsArgs]):
    name = "search_documents"
    description = (
        "Search the user's own documents (the folders they chose for 'Ask my documents') by "
        "meaning and keywords. Returns the most relevant passages with file paths. Use it to "
        "answer questions about their files; cite the file names in your answer and open a "
        "file with read_file when you need more of it."
    )
    args_model: ClassVar[type[ToolArgs]] = SearchDocumentsArgs
    risk = Risk.READ

    async def run(self, args: SearchDocumentsArgs, ctx: ToolContext) -> ToolResult:
        index = ctx.documents
        if index is None:
            raise ToolError("'Ask my documents' is turned off; enable it in Settings → Features")
        folders = ctx.settings.features.documents.folders or list(ctx.guard.allowed)
        note = ""
        if time.time() - index.last_sync > REFRESH_AFTER_S:
            report = await asyncio.to_thread(index.sync, folders, ctx.guard, SEARCH_SYNC_BUDGET_S)
            if report.pending:
                note = (
                    f"\n(Still indexing: {report.pending} files not searched yet; "
                    "results will improve shortly.)"
                )
            if report.skipped_folders:
                note += f"\n(Skipped folders outside the allowed roots: {report.skipped_folders})"
        hits = await asyncio.to_thread(index.search, args.query, args.limit)
        if not hits:
            return ToolResult("No matching passages in the indexed documents." + note)
        parts = [f"[{i + 1}] {h.path} (part {h.chunk + 1})\n{h.text}" for i, h in enumerate(hits)]
        return ToolResult("\n\n".join(parts) + note, untrusted=True, source="the user's documents")


KNOWLEDGE_TOOLS: list[Tool[Any]] = [SearchDocuments()]
