"""Assemble an Agent from settings."""

from __future__ import annotations

import sys
from typing import Any

from jarvis.agent.builtin_tools import BUILTIN_TOOLS
from jarvis.agent.events import EventSink, null_sink
from jarvis.agent.loop import Agent
from jarvis.core.audit import AuditLog
from jarvis.core.config import ScreenControl, Settings
from jarvis.core.paths import AppPaths
from jarvis.desktop.session import DesktopSession
from jarvis.llm import create_provider
from jarvis.llm.base import LLMProvider
from jarvis.llm.budget import SpendMeter
from jarvis.memory.store import Store
from jarvis.safety.policy import Approver, PathGuard
from jarvis.tools.base import Tool, ToolContext, ToolRegistry
from jarvis.tools.desktop import DESKTOP_TOOLS
from jarvis.tools.execution import EXEC_TOOLS
from jarvis.tools.files import FILE_TOOLS
from jarvis.tools.mail import EMAIL_TOOLS
from jarvis.tools.memory import MEMORY_TOOLS
from jarvis.tools.office import OFFICE_TOOLS
from jarvis.tools.web import WEB_TOOLS


def all_tools(desktop: bool = False, memory: bool = False) -> list[Tool[Any]]:
    tools = [*BUILTIN_TOOLS, *FILE_TOOLS, *OFFICE_TOOLS, *EMAIL_TOOLS, *EXEC_TOOLS, *WEB_TOOLS]
    if memory:
        tools += MEMORY_TOOLS
    if desktop:
        tools += DESKTOP_TOOLS
    return tools


def open_meter(settings: Settings, paths: AppPaths) -> SpendMeter:
    """Daily API spend tracking (shared across JARVIS processes via SQLite)."""
    return SpendMeter(settings.budget, paths.data_dir / "usage.db")


def open_store(settings: Settings, paths: AppPaths) -> Store | None:
    if not settings.memory.enabled:
        return None
    store = Store(paths.data_dir / "jarvis.db")
    store.mark_interrupted()
    return store


def build_desktop(settings: Settings) -> DesktopSession | None:
    """Screen tools are Windows-only and can be switched off entirely in settings."""
    if sys.platform != "win32" or settings.desktop.screen_control is ScreenControl.DENY:
        return None
    from jarvis.desktop.computer import ComputerController, Win32Input
    from jarvis.desktop.screen import ScreenCapture
    from jarvis.desktop.uia import UIAService

    cfg = settings.desktop
    screen = ScreenCapture(cfg.monitor, cfg.max_screenshot_edge)
    return DesktopSession(
        cfg,
        UIAService(cfg.blocked_windows, monitor_area=screen.monitor_area),
        ComputerController(screen, Win32Input()),
        kill_hotkey=settings.safety.kill_hotkey,
    )


def build_agent(
    settings: Settings,
    paths: AppPaths,
    approver: Approver,
    emit: EventSink = null_sink,
    provider: LLMProvider | None = None,
    store: Store | None = None,
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
        desktop=build_desktop(settings),
        store=store if store is not None else open_store(settings, paths),
    )
    provider = provider or create_provider(settings.llm, open_meter(settings, paths))
    tools = all_tools(desktop=ctx.desktop is not None, memory=ctx.store is not None)
    agent = Agent(provider, ToolRegistry(tools), ctx, settings.agent, emit)
    return agent, provider
