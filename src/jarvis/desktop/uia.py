"""Windows UI Automation: read app controls as a tree and act on them by id.

All UIA/COM work runs on one dedicated thread (COM objects are apartment-bound), so the
element ids handed to the model stay valid between an ``inspect`` and a later ``click``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import fnmatch
import json
import logging
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

MAX_NODES = 600
VALUE_TYPES = {"EditControl", "DocumentControl", "ComboBoxControl", "SpinnerControl"}


class UIAError(Exception):
    pass


@dataclass(frozen=True)
class WindowInfo:
    handle: int
    title: str
    process: str
    pid: int


@dataclass(frozen=True)
class StartApp:
    name: str
    app_id: str


def escape_sendkeys(text: str) -> str:
    """Escape braces for uiautomation's SendKeys syntax ({ -> {{}, } -> {}})."""
    return "".join("{{}" if ch == "{" else "{}}" if ch == "}" else ch for ch in text)


def _rect(c: Any) -> str:
    r = c.BoundingRectangle
    if r.width() <= 0 or r.height() <= 0:
        return ""
    return f" @({r.left},{r.top} {r.width()}x{r.height()})"


class UIAService:
    def __init__(
        self,
        blocked_title_patterns: list[str],
        monitor_area: Callable[[], tuple[int, int, int, int]] | None = None,
    ) -> None:
        self._blocked = [p.lower() for p in blocked_title_patterns]
        # The monitor JARVIS sees; windows it focuses are brought onto it.
        self._monitor_area = monitor_area
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="uia", initializer=self._init_thread
        )
        self._elements: dict[int, Any] = {}
        self._element_window = ""
        self._apps: list[StartApp] | None = None

    @staticmethod
    def _init_thread() -> None:
        import uiautomation as auto

        auto.InitializeUIAutomationInCurrentThread()

    async def _call[T](self, fn: Callable[[], T]) -> T:
        return await asyncio.get_running_loop().run_in_executor(self._pool, fn)

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------ protection

    def is_blocked(self, title: str) -> bool:
        t = title.lower()
        return any(fnmatch.fnmatch(t, p) for p in self._blocked)

    def _check(self, title: str) -> None:
        if self.is_blocked(title):
            raise UIAError(
                f"'{title}' is a protected window (safety.blocked_windows); JARVIS won't read "
                "or operate it. Ask the user to handle it."
            )

    async def foreground_title(self) -> str:
        from jarvis.desktop.winapi import foreground_window

        return (await asyncio.to_thread(foreground_window))[1]

    async def focused_is_password(self) -> bool:
        def probe() -> bool:
            import uiautomation as auto

            try:
                c = auto.GetFocusedControl()
                return bool(c and c.IsPassword)
            except Exception:  # UIA can throw for transient elements
                return False

        return await self._call(probe)

    # ------------------------------------------------------------ windows

    async def list_windows(self) -> list[WindowInfo]:
        def collect() -> list[WindowInfo]:
            import uiautomation as auto

            from jarvis.desktop.winapi import process_name

            out = []
            for w in auto.GetRootControl().GetChildren():
                title = w.Name or ""
                if not title.strip() or w.IsOffscreen:
                    continue
                out.append(
                    WindowInfo(w.NativeWindowHandle, title, process_name(w.ProcessId), w.ProcessId)
                )
            return out

        return await self._call(collect)

    def _find_window(self, title: str) -> Any:
        import uiautomation as auto

        wanted = title.lower().strip()
        candidates = [
            w
            for w in auto.GetRootControl().GetChildren()
            if (w.Name or "").strip() and not w.IsOffscreen
        ]
        for match in (
            lambda n: n == wanted,
            lambda n: n.startswith(wanted),
            lambda n: wanted in n,
        ):
            hits = [w for w in candidates if match(w.Name.lower())]
            if hits:
                return hits[0]
        names = ", ".join(repr(w.Name) for w in candidates[:15])
        raise UIAError(f"no window matching {title!r}. Open windows: {names}")

    async def focus_window(self, title: str) -> str:
        def run() -> str:
            w = self._find_window(title)
            self._check(w.Name)
            if hasattr(w, "SetActive"):
                w.SetActive()
            else:
                w.SetFocus()
            time.sleep(0.3)
            note = self._bring_onto_screen(int(w.NativeWindowHandle))
            return str(w.Name) + note

        return await self._call(run)

    def _bring_onto_screen(self, hwnd: int) -> str:
        if self._monitor_area is None or not hwnd:
            return ""
        from jarvis.desktop import winapi

        ax, ay, aw, ah = self._monitor_area()
        left, top, right, bottom = winapi.window_rect(hwnd)
        cx, cy = (left + right) // 2, (top + bottom) // 2
        if ax <= cx < ax + aw and ay <= cy < ay + ah:
            return ""
        winapi.move_window_into(hwnd, (ax, ay, aw, ah))
        time.sleep(0.3)
        return " (moved it from another monitor onto the screen JARVIS controls)"

    # ------------------------------------------------------------ inspect / act

    async def inspect(self, title: str | None, max_depth: int) -> str:
        def run() -> str:
            import uiautomation as auto

            if title:
                win = self._find_window(title)
            else:
                win = auto.GetForegroundControl().GetTopLevelControl()
            self._check(win.Name or "")
            self._elements = {}
            self._element_window = win.Name or ""
            lines = [f'Window "{win.Name}" (pid {win.ProcessId})']
            truncated = False

            def walk(c: Any, depth: int) -> None:
                nonlocal truncated
                if len(self._elements) >= MAX_NODES:
                    truncated = True
                    return
                idx = len(self._elements)
                self._elements[idx] = c
                name = (c.Name or "").replace("\n", " ")[:80]
                extra = ""
                if c.ControlTypeName in VALUE_TYPES:
                    if getattr(c, "IsPassword", False):
                        extra = ' value="••••"'
                    else:
                        try:
                            vp = c.GetValuePattern()
                            val = (vp.Value or "")[:80].replace("\n", " ")
                            extra = f' value="{val}"'
                        except Exception:
                            extra = ""
                if not c.IsEnabled:
                    extra += " (disabled)"
                auto_id = f" id={c.AutomationId}" if c.AutomationId else ""
                ctype = c.ControlTypeName.removesuffix("Control")
                lines.append(f'{"  " * depth}[{idx}] {ctype} "{name}"{auto_id}{extra}{_rect(c)}')
                if depth >= max_depth:
                    return
                try:
                    children = c.GetChildren()
                except Exception:
                    return
                for child in children:
                    walk(child, depth + 1)

            walk(win, 0)
            if truncated:
                lines.append(
                    f"[... truncated at {MAX_NODES} elements; inspect with lower depth ...]"
                )
            return "\n".join(lines)

        return await self._call(run)

    def _element(self, element_id: int) -> Any:
        c = self._elements.get(element_id)
        if c is None:
            raise UIAError(f"unknown element id {element_id}; run inspect_window first")
        self._check(self._element_window)
        return c

    async def click(self, element_id: int, double: bool = False) -> str:
        def run() -> str:
            c = self._element(element_id)
            label = f'{c.ControlTypeName.removesuffix("Control")} "{c.Name}"'
            if not double:
                for getter, action, verb in (
                    ("GetInvokePattern", "Invoke", "Invoked"),
                    ("GetTogglePattern", "Toggle", "Toggled"),
                    ("GetSelectionItemPattern", "Select", "Selected"),
                    ("GetExpandCollapsePattern", "Expand", "Expanded"),
                ):
                    try:
                        pattern = getattr(c, getter)()
                    except Exception:
                        pattern = None
                    if pattern:
                        getattr(pattern, action)()
                        return f"{verb} {label}"
            r = c.BoundingRectangle
            if r.width() <= 0 or r.height() <= 0:
                raise UIAError(f"{label} has no on-screen area to click")
            c.Click(simulateMove=False) if not double else c.DoubleClick(simulateMove=False)
            return f"{'Double-clicked' if double else 'Clicked'} {label}"

        return await self._call(run)

    async def set_text(self, element_id: int, text: str) -> str:
        def run() -> str:
            c = self._element(element_id)
            if getattr(c, "IsPassword", False):
                raise UIAError(
                    "that is a password field; JARVIS never types passwords. Ask the user."
                )
            label = f'{c.ControlTypeName.removesuffix("Control")} "{c.Name}"'
            try:
                vp = c.GetValuePattern()
            except Exception:
                vp = None
            if vp is not None and not vp.IsReadOnly:
                vp.SetValue(text)
                return f"Set {label} to {len(text)} characters (verified: {vp.Value == text})"
            c.SetFocus()
            c.SendKeys("{Ctrl}a", waitTime=0.05)
            c.SendKeys(escape_sendkeys(text), interval=0.005, waitTime=0.1)
            return f"Typed {len(text)} characters into {label}"

        return await self._call(run)

    # ------------------------------------------------------------ apps

    async def start_apps(self) -> list[StartApp]:
        if self._apps is None:

            def load() -> list[StartApp]:
                argv = [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress",
                ]
                out = subprocess.run(  # noqa: S603 - fixed command, no user input
                    argv,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                data = json.loads(out.stdout or "[]")
                if isinstance(data, dict):
                    data = [data]
                return [StartApp(d["Name"], d["AppID"]) for d in data if d.get("AppID")]

            self._apps = await asyncio.to_thread(load)
        return self._apps

    async def launch(self, name: str) -> str:
        apps = await self.start_apps()
        wanted = name.lower().strip()
        app = (
            next((a for a in apps if a.name.lower() == wanted), None)
            or next((a for a in apps if a.name.lower().startswith(wanted)), None)
            or next((a for a in apps if wanted in a.name.lower()), None)
        )
        if app is None:
            close = [a.name for a in apps if any(w in a.name.lower() for w in wanted.split())][:10]
            raise UIAError(f"no installed app named {name!r}. Similar: {close or 'none'}")
        await asyncio.to_thread(
            subprocess.Popen,
            ["explorer.exe", f"shell:AppsFolder\\{app.app_id}"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        await asyncio.sleep(1.5)
        return f"Launched {app.name}"
