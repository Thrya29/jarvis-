"""Run the JARVIS service: API + UI server, optional tray icon and window.

`jarvis run` serves headless. `jarvis app` serves with a tray icon and opens the
desktop window; if JARVIS is already running it just opens the window.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Any

import uvicorn

from jarvis import __version__
from jarvis.core.audit import AuditLog
from jarvis.core.config import Settings
from jarvis.core.instance import AlreadyRunningError, InstanceLock, load_or_create_token
from jarvis.core.paths import AppPaths
from jarvis.server.app import create_app
from jarvis.server.hub import Hub

log = logging.getLogger(__name__)


def _healthy(settings: Settings, timeout: float = 1.0) -> bool:
    url = f"http://{settings.server.host}:{settings.server.port}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return bool(resp.status == 200)
    except (urllib.error.URLError, OSError):
        return False


class _Tray:
    """System tray icon: open window, voice on/off, stop, quit."""

    def __init__(self, hub: Hub, server: uvicorn.Server, open_ui: Any) -> None:
        self._hub = hub
        self._server = server
        self._open_ui = open_ui
        self._icon: Any = None

    def start(self) -> None:
        import pystray

        from jarvis.app.icon import icon_image

        def call(coro_fn: Any) -> None:
            if self._hub.loop is not None:
                asyncio.run_coroutine_threadsafe(coro_fn(), self._hub.loop)

        def toggle_voice(icon: Any, item: Any) -> None:
            call(lambda: self._hub.set_voice_enabled(not self._hub.voice_running))

        def stop_tasks(icon: Any, item: Any) -> None:
            if self._hub.loop is not None:
                self._hub.loop.call_soon_threadsafe(self._hub.board.cancel_all)

        def quit_app(icon: Any, item: Any) -> None:
            self._server.should_exit = True
            icon.stop()

        menu = pystray.Menu(
            pystray.MenuItem("Open JARVIS", lambda icon, item: self._open_ui(), default=True),
            pystray.MenuItem(
                "Voice (Hey Jarvis)", toggle_voice, checked=lambda item: self._hub.voice_running
            ),
            pystray.MenuItem(
                f"Stop current task ({self._hub.settings.safety.kill_hotkey.upper()})", stop_tasks
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit JARVIS", quit_app),
        )
        self._icon = pystray.Icon("jarvis", icon_image(64), f"JARVIS {__version__}", menu)
        threading.Thread(target=self._icon.run, name="tray", daemon=True).start()

    def stop(self) -> None:
        if self._icon is not None:
            self._icon.stop()


def serve(settings: Settings, paths: AppPaths, *, desktop: bool = False, show: bool = True) -> int:
    """Run until stopped. With desktop=True: tray icon, and open the window (if show)."""
    from jarvis.app.window import open_window

    token = load_or_create_token(paths.token_file)
    port = settings.server.port

    def open_ui() -> None:
        open_window(paths, port, token)

    try:
        lock = InstanceLock(paths.lock_file)
        lock.acquire()
    except AlreadyRunningError:
        if desktop and _healthy(settings):
            # JARVIS is already running: show its window (unless this is the
            # minimized autostart launch, which should do nothing).
            if show:
                open_ui()
            return 0
        raise

    try:
        audit = AuditLog(paths.audit_log)
        hub = Hub(settings, paths)
        app = create_app(settings, token, kill_switch=True, hub=hub)
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=settings.server.host,
                port=port,
                log_config=None,
                access_log=False,
                ws_max_size=4 * 1024 * 1024,
            )
        )
        tray = _Tray(hub, server, open_ui) if desktop else None

        def after_start() -> None:
            deadline = time.monotonic() + 30
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.1)
            if server.started and desktop and show:
                open_ui()

        audit.record("daemon.start", version=__version__)
        log.info("JARVIS %s listening on http://%s:%d", __version__, settings.server.host, port)
        if tray is not None:
            tray.start()
        threading.Thread(target=after_start, name="open-ui", daemon=True).start()
        try:
            server.run()
        finally:
            if tray is not None:
                tray.stop()
            audit.record("daemon.stop")
            log.info("JARVIS stopped")
        return 0
    finally:
        lock.release()


def run_daemon(settings: Settings, paths: AppPaths) -> None:
    serve(settings, paths, desktop=False)
