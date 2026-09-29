"""Thin ctypes layer over the Win32 input and window APIs.

Coordinates are physical pixels; call :func:`enable_dpi_awareness` once at startup so
screenshots, cursor positions and UI Automation rectangles all agree.
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

IS_WINDOWS = sys.platform == "win32"

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
WHEEL_DELTA = 120

BUTTON_FLAGS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}

ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _user32() -> ctypes.WinDLL:
    if not IS_WINDOWS:
        raise OSError("desktop control is only available on Windows")
    return ctypes.WinDLL("user32", use_last_error=True)


def enable_dpi_awareness() -> None:
    """Per-monitor v2 DPI awareness, so all coordinates are physical pixels."""
    if not IS_WINDOWS:
        return
    user32 = _user32()
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 == -4
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except AttributeError:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        user32.SetProcessDPIAware()


def _send(*inputs: INPUT) -> None:
    arr = (INPUT * len(inputs))(*inputs)
    sent = _user32().SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
    if sent != len(inputs):
        raise OSError(
            f"SendInput blocked ({ctypes.get_last_error()}); the target may be an elevated "
            "window or the secure desktop"
        )


def _mouse(flags: int, data: int = 0) -> INPUT:
    return INPUT(INPUT_MOUSE, _INPUTUNION(mi=MOUSEINPUT(0, 0, data & 0xFFFFFFFF, flags, 0, 0)))


def _key(vk: int, up: bool, extended: bool = False) -> INPUT:
    flags = (KEYEVENTF_KEYUP if up else 0) | (KEYEVENTF_EXTENDEDKEY if extended else 0)
    return INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=KEYBDINPUT(vk, 0, flags, 0, 0)))


def _unicode(ch: int, up: bool) -> INPUT:
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
    return INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=KEYBDINPUT(0, ch, flags, 0, 0)))


def cursor_pos() -> tuple[int, int]:
    pt = wintypes.POINT()
    _user32().GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def move_to(x: int, y: int) -> None:
    if not _user32().SetCursorPos(int(x), int(y)):
        raise OSError(f"cannot move the cursor to {x},{y}")


def button(name: str, down: bool) -> None:
    down_flag, up_flag = BUTTON_FLAGS[name]
    _send(_mouse(down_flag if down else up_flag))


def click(name: str, count: int = 1) -> None:
    down_flag, up_flag = BUTTON_FLAGS[name]
    for i in range(count):
        _send(_mouse(down_flag), _mouse(up_flag))
        if i + 1 < count:
            time.sleep(0.05)


def wheel(clicks: int, horizontal: bool = False) -> None:
    flag = MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL
    _send(_mouse(flag, clicks * WHEEL_DELTA))


def key_event(vk: int, up: bool, extended: bool = False) -> None:
    _send(_key(vk, up, extended))


def type_unicode(text: str) -> None:
    # UTF-16 code units, so characters outside the BMP (emoji) arrive as surrogate pairs.
    units = text.encode("utf-16-le")
    codes = [int.from_bytes(units[i : i + 2], "little") for i in range(0, len(units), 2)]
    for code in codes:
        _send(_unicode(code, False), _unicode(code, True))


def vk_for_char(ch: str) -> tuple[int, bool] | None:
    """Virtual key + whether Shift is needed, for the current keyboard layout."""
    res = _user32().VkKeyScanW(ctypes.c_wchar(ch))
    if res == -1:
        return None
    return res & 0xFF, bool(res & 0x100)


def foreground_window() -> tuple[int, str, int]:
    """(hwnd, title, pid) of the foreground window."""
    user32 = _user32()
    hwnd = user32.GetForegroundWindow()
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(hwnd or 0), buf.value, int(pid.value)


def process_name(pid: int) -> str:
    """Executable file name for a process id ("" if it can't be read)."""
    if not IS_WINDOWS:
        return ""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return buf.value.rsplit("\\", 1)[-1]
    finally:
        kernel32.CloseHandle(handle)


SW_MAXIMIZE = 3
SW_RESTORE = 9
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    """(left, top, right, bottom) of a window in physical pixels."""
    r = wintypes.RECT()
    if not _user32().GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r)):
        raise OSError(f"cannot read window rectangle ({ctypes.get_last_error()})")
    return r.left, r.top, r.right, r.bottom


def is_maximized(hwnd: int) -> bool:
    return bool(_user32().IsZoomed(wintypes.HWND(hwnd)))


def move_window_into(hwnd: int, area: tuple[int, int, int, int]) -> None:
    """Move a window onto the given monitor area, keeping it maximized if it was."""
    user32 = _user32()
    h = wintypes.HWND(hwnd)
    was_max = is_maximized(hwnd)
    if was_max:
        user32.ShowWindow(h, SW_RESTORE)
    left, top, right, bottom = window_rect(hwnd)
    ax, ay, aw, ah = area
    w = min(right - left, aw)
    height = min(bottom - top, ah)
    x = ax + max(0, (aw - w) // 2)
    y = ay + max(0, (ah - height) // 2)
    if not user32.SetWindowPos(h, None, x, y, w, height, SWP_NOZORDER | SWP_NOACTIVATE):
        raise OSError(f"cannot move window ({ctypes.get_last_error()})")
    if was_max:
        user32.ShowWindow(h, SW_MAXIMIZE)
