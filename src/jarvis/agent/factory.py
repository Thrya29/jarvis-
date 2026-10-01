"""Assemble an Agent from settings."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from jarvis.agent.builtin_tools import BUILTIN_TOOLS
from jarvis.agent.events import EventSink, null_sink
from jarvis.agent.loop import Agent
from jarvis.connect.accounts import ConnectionManager
from jarvis.core.audit import AuditLog
from jarvis.core.config import ScreenControl, Settings
from jarvis.core.paths import AppPaths
from jarvis.desktop.session import DesktopSession
from jarvis.knowledge.index import DocIndex, Embedder
from jarvis.llm import create_provider
from jarvis.llm.base import LLMProvider
from jarvis.llm.budget import SpendMeter
from jarvis.maps.service import MapService
from jarvis.memory.store import Store
from jarvis.safety.policy import Approver, PathGuard
from jarvis.tools.base import Tool, ToolContext, ToolRegistry
from jarvis.tools.connected import CONNECTED_TOOLS
from jarvis.tools.desktop import DESKTOP_TOOLS
from jarvis.tools.execution import EXEC_TOOLS
from jarvis.tools.files import FILE_TOOLS
from jarvis.tools.knowledge import KNOWLEDGE_TOOLS
from jarvis.tools.mail import EMAIL_TOOLS
from jarvis.tools.maps import MAP_TOOLS
from jarvis.tools.memory import MEMORY_TOOLS
from jarvis.tools.office import OFFICE_TOOLS
from jarvis.tools.web import WEB_TOOLS


def all_tools(
    desktop: bool = False,
    memory: bool = False,
    documents: bool = False,
    connected: bool = False,
    maps: bool = False,
) -> list[Tool[Any]]:
    tools = [*BUILTIN_TOOLS, *FILE_TOOLS, *OFFICE_TOOLS, *EMAIL_TOOLS, *EXEC_TOOLS, *WEB_TOOLS]
    if memory:
        tools += MEMORY_TOOLS
    if documents:
        tools += KNOWLEDGE_TOOLS
    if connected:
        tools += CONNECTED_TOOLS
    if maps:
        tools += MAP_TOOLS
    if desktop:
        tools += DESKTOP_TOOLS
    return tools


def open_meter(settings: Settings, paths: AppPaths) -> SpendMeter:
    """Daily API spend tracking (shared across JARVIS processes via SQLite)."""
    return SpendMeter(settings.budget, paths.data_dir / "usage.db")


_DOC_INDEXES: dict[Path, DocIndex] = {}


def open_documents(settings: Settings, paths: AppPaths) -> DocIndex | None:
    """The shared document index, if the feature is on and its model is installed."""
    from jarvis.voice.runtime import document_files, model_store

    if not settings.features.documents.enabled:
        return None
    models = model_store(paths)
    if models.missing(document_files(settings)):
        return None
    db = paths.data_dir / "documents.db"
    if db not in _DOC_INDEXES:
        embedder = Embedder(
            models.path("bge-small-en-v1.5-int8.onnx"),
            models.path("bge-small-en-v1.5-tokenizer.json"),
        )
        _DOC_INDEXES[db] = DocIndex(db, embedder)
    return _DOC_INDEXES[db]


_CONNECTIONS: dict[Path, tuple[ConnectionManager, list[Settings]]] = {}


def open_connections(settings: Settings, paths: AppPaths) -> ConnectionManager:
    """The shared connection manager; it always reads the newest settings it was given."""
    key = paths.data_dir
    if key not in _CONNECTIONS:
        holder = [settings]
        manager = ConnectionManager(lambda: holder[0].connections, paths.data_dir)
        _CONNECTIONS[key] = (manager, holder)
    manager, holder = _CONNECTIONS[key]
    holder[0] = settings
    return manager


_MAPS: dict[Path, tuple[MapService, list[Settings]]] = {}


def open_maps(settings: Settings, paths: AppPaths) -> MapService:
    """The shared map service (caches are per process, not per agent)."""
    key = paths.data_dir
    if key not in _MAPS:
        holder = [settings]
        service = MapService(
            lambda: holder[0],
            paths.data_dir,
            connections=lambda: open_connections(holder[0], paths),
        )
        _MAPS[key] = (service, holder)
    service, holder = _MAPS[key]
    holder[0] = settings
    return service


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
        documents=open_documents(settings, paths),
        connections=open_connections(settings, paths),
        maps=open_maps(settings, paths) if settings.features.maps.enabled else None,
    )
    provider = provider or create_provider(settings.llm, open_meter(settings, paths))
    tools = all_tools(
        desktop=ctx.desktop is not None,
        memory=ctx.store is not None,
        documents=ctx.documents is not None,
        connected=bool(ctx.connections.accounts()),
        maps=ctx.maps is not None,
    )
    agent = Agent(provider, ToolRegistry(tools), ctx, settings.agent, emit)
    return agent, provider
