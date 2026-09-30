"""The desktop window: Microsoft Edge in app mode pointed at the local UI.

Edge ships with Windows 10/11, so this needs no extra runtime. A dedicated browser
profile under JARVIS's data folder keeps the window separate from the user's normal
browsing (history, extensions, cookies). The API token travels in the URL fragment,
which is never sent to the server.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import webbrowser
from pathlib import Path

from jarvis.core.paths import AppPaths

log = logging.getLogger(__name__)


def find_edge() -> Path | None:
    candidates: list[Path] = []
    if sys.platform == "win32":
        import winreg

        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(
                    hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"
                ) as key:
                    candidates.append(Path(winreg.QueryValue(key, None)))
            except OSError:
                pass
    for env in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            candidates.append(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    return next((c for c in candidates if c.is_file()), None)


def ui_url(port: int, token: str) -> str:
    return f"http://127.0.0.1:{port}/#token={token}"


def edge_command(edge: Path, url: str, profile: Path) -> list[str]:
    return [
        str(edge),
        f"--app={url}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,msEdgeSidebarV2,msUndersideButton",
        "--window-size=1240,820",
    ]


def open_window(paths: AppPaths, port: int, token: str) -> None:
    url = ui_url(port, token)
    edge = find_edge()
    if edge is None:
        log.warning("Microsoft Edge not found; opening the UI in the default browser")
        webbrowser.open(url)
        return
    profile = paths.data_dir / "ui-profile"
    profile.mkdir(parents=True, exist_ok=True)
    flags = subprocess.DETACHED_PROCESS if sys.platform == "win32" else 0
    subprocess.Popen(  # noqa: S603 - fixed browser binary, our own URL
        edge_command(edge, url, profile),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
        close_fds=True,
    )
