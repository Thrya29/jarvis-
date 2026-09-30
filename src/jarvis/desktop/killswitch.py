"""System-wide kill hotkey (default Ctrl+Alt+J) that stops whatever JARVIS is doing.

A dedicated thread owns a Win32 message loop with ``RegisterHotKey``; pressing the
combo from any application calls the callback.
"""

from __future__ import annotations

import ctypes
import logging
import threading
from collections.abc import Callable
from ctypes import wintypes

from jarvis.desktop import keys
from jarvis.desktop.winapi import IS_WINDOWS

log = logging.getLogger(__name__)

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
_MOD_FLAGS = {0x12: MOD_ALT, 0x11: MOD_CONTROL, 0x10: MOD_SHIFT, 0x5B: MOD_WIN}
HOTKEY_ID = 0x4A52  # "JR"


def hotkey_spec(combo: str) -> tuple[int, int]:
    stroke = keys.parse_combo(combo)
    if stroke.vk is None or not stroke.modifiers:
        raise keys.KeySpecError("kill hotkey needs at least one modifier and a key")
    mods = MOD_NOREPEAT
    for m in stroke.modifiers:
        mods |= _MOD_FLAGS[m]
    return mods, stroke.vk


class KillSwitch:
    def __init__(self, combo: str, on_press: Callable[[], object]) -> None:
        self._mods, self._vk = hotkey_spec(combo)
        self._combo = combo
        self._on_press = on_press
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self.active = False

    def start(self) -> bool:
        if not IS_WINDOWS:
            return False
        self._thread = threading.Thread(target=self._run, name="killswitch", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=2)
        return self.active

    def _run(self) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32")
        self._thread_id = kernel32.GetCurrentThreadId()
        if not user32.RegisterHotKey(None, HOTKEY_ID, self._mods, self._vk):
            log.warning("could not register kill hotkey %s (in use by another app?)", self._combo)
            self._ready.set()
            return
        self.active = True
        self._ready.set()
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                    log.warning("kill hotkey pressed")
                    try:
                        self._on_press()
                    except Exception:
                        log.exception("kill switch callback failed")
        finally:
            user32.UnregisterHotKey(None, HOTKEY_ID)
            self.active = False

    def stop(self) -> None:
        if self._thread and self._thread.is_alive() and self._thread_id:
            ctypes.WinDLL("user32").PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            self._thread.join(timeout=2)
