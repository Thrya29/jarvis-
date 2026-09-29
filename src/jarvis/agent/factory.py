"""Assemble an Agent from settings."""

from __future__ import annotations

from typing import Any

from jarvis.agent.builtin_tools import BUILTIN_TOOLS
from jarvis.agent.events import EventSink, null_sink
from jarvis.agent.loop import Agent
from jarvis.core.audit import AuditLog
from jarvis.core.config import Settings
from jarvis.core.paths import AppPaths
from jarvis.llm import create_provider
from jarvis.llm.base import LLMProvider
from jarvis.safety.policy import Approver, PathGuard
from jarvis.tools.base import Tool, ToolContext, ToolRegistry
from jarvis.tools.execution import EXEC_TOOLS
from jarvis.tools.files import FILE_TOOLS
from jarvis.tools.mail import EMAIL_TOOLS
from jarvis.tools.office import OFFICE_TOOLS
from jarvis.tools.web import WEB_TOOLS


def all_tools() -> list[Tool[Any]]:
    return [*BUILTIN_TOOLS, *FILE_TOOLS, *OFFICE_TOOLS, *EMAIL_TOOLS, *EXEC_TOOLS, *WEB_TOOLS]


def build_agent(
    settings: Settings,
    paths: AppPaths,
    approver: Approver,
    emit: EventSink = null_sink,
    provider: LLMProvider | None = None,
) -> tuple[Agent, LLMProvider]:
    guard = PathGuard(
        settings.safety.allowed_roots,
        # JARVIS's own config, token and logs are never reachable through tools.
        deny_roots=[paths.config_dir, paths.data_dir, paths.log_dir],
    )
    ctx = ToolContext(
        settings=settings,
        guard=guard,
        audit=AuditLog(paths.audit_log),
        approver=approver,
        work_dir=paths.cache_dir / "work",
    )
    provider = provider or create_provider(settings.llm)
    agent = Agent(provider, ToolRegistry(all_tools()), ctx, settings.agent, emit)
    return agent, provider
