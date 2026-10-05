"""The floating overlay: a small see-through, always-on-top, click-through window.

It is a separate native program (``jarvis-overlay.exe``, Tauri/WebView2) that shows one
page from the local JARVIS service: core state, the current step, the latest reply,
and approvals. JARVIS starts it with the page URL and its own process id; the overlay
refuses any non-local URL and exits by itself when JARVIS exits.

Hotkeys (handled by the overlay): Ctrl+Alt+O makes it clickable (to approve or open
JARVIS), Ctrl+Alt+H hides or shows it.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

EXE_NAME = "jarvis-overlay.exe"
INTERACT_HOTKEY = "Ctrl+Alt+O"
HIDE_HOTKEY = "Ctrl+Alt+H"


def find_overlay() -> Path | None:
    """The overlay program: next to the frozen app, or a local cargo build in a checkout."""
    candidates: list[Path] = []
    override = os.environ.get("JARVIS_OVERLAY_EXE")
    if override:
        candidates.append(Path(override))
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).parent / EXE_NAME)
    repo = Path(__file__).resolve().parents[3]
    candidates.append(repo / "overlay" / "target" / "release" / EXE_NAME)
    return next((c for c in candidates if c.is_file()), None)


def overlay_url(port: int, token: str) -> str:
    return f"http://127.0.0.1:{port}/ui/overlay.html#token={token}"


class Overlay:
    """Starts and stops the overlay process."""

    def __init__(self, port: int, token: str, exe: Path | None = None) -> None:
        self._port = port
        self._token = token
        self._exe = exe
        self._proc: subprocess.Popen[bytes] | None = None

    @property
    def exe(self) -> Path | None:
        return self._exe or find_overlay()

    @property
    def available(self) -> bool:
        return self.exe is not None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        if self.running:
            return
        exe = self.exe
        if exe is None:
            raise OSError("the overlay isn't installed (jarvis-overlay.exe not found)")
        cmd = [
            str(exe),
            "--url",
            overlay_url(self._port, self._token),
            "--parent-pid",
            str(os.getpid()),
        ]
        self._proc = subprocess.Popen(  # noqa: S603 - our own program, fixed arguments
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        log.info("overlay started (pid %d)", self._proc.pid)

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.info("overlay stopped")
