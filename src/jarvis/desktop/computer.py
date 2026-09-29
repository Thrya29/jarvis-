"""Executor for Claude's computer-use toolset (``computer_toolset_20260801``).

Each member tool (``screenshot``, ``left_click``, ``type``, ...) maps to one method here.
Input goes through an :class:`InputBackend` so the logic is testable without driving
the real mouse and keyboard.
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass
from typing import Any, Protocol

from jarvis.desktop import keys
from jarvis.desktop.screen import ScreenCapture, ScreenMap

TOOLSET_NAME = "computer"
MEMBERS = frozenset(
    {
        "screenshot", "zoom", "left_click", "right_click", "middle_click", "double_click",
        "triple_click", "left_click_drag", "mouse_move", "left_mouse_down", "left_mouse_up",
        "cursor_position", "scroll", "type", "key", "hold_key", "wait",
    }
)  # fmt: skip
OBSERVE_ONLY = frozenset({"screenshot", "zoom", "cursor_position", "wait"})
MAX_WAIT_S = 30.0  # the API allows 300 s; a stuck agent shouldn't idle that long
TYPE_CHUNK = 50


class ComputerError(Exception):
    pass


@dataclass(frozen=True)
class ActionResult:
    text: str = "OK"
    png: bytes | None = None


class InputBackend(Protocol):
    def move_to(self, x: int, y: int) -> None: ...
    def cursor_pos(self) -> tuple[int, int]: ...
    def button(self, name: str, down: bool) -> None: ...
    def click(self, name: str, count: int) -> None: ...
    def wheel(self, clicks: int, horizontal: bool) -> None: ...
    def key_event(self, vk: int, up: bool, extended: bool = False) -> None: ...
    def type_unicode(self, text: str) -> None: ...
    def vk_for_char(self, ch: str) -> tuple[int, bool] | None: ...


class Win32Input:
    """The real backend."""

    def __init__(self) -> None:
        from jarvis.desktop import winapi

        self._w = winapi

    def move_to(self, x: int, y: int) -> None:
        self._w.move_to(x, y)

    def cursor_pos(self) -> tuple[int, int]:
        return self._w.cursor_pos()

    def button(self, name: str, down: bool) -> None:
        self._w.button(name, down)

    def click(self, name: str, count: int) -> None:
        self._w.click(name, count)

    def wheel(self, clicks: int, horizontal: bool) -> None:
        self._w.wheel(clicks, horizontal)

    def key_event(self, vk: int, up: bool, extended: bool = False) -> None:
        self._w.key_event(vk, up, extended)

    def type_unicode(self, text: str) -> None:
        self._w.type_unicode(text)

    def vk_for_char(self, ch: str) -> tuple[int, bool] | None:
        return self._w.vk_for_char(ch)


class ComputerController:
    def __init__(self, screen: ScreenCapture, backend: InputBackend, settle_s: float = 0.15):
        self.screen = screen
        self.io = backend
        self._settle = settle_s
        self._held: set[int] = set()

    # ------------------------------------------------------------------ helpers

    def _map(self) -> ScreenMap:
        return self.screen.current_map()

    def _point(self, coord: Any) -> tuple[int, int]:
        if (
            not isinstance(coord, list | tuple)
            or len(coord) != 2
            or not all(isinstance(v, int | float) for v in coord)
        ):
            raise ComputerError(f"coordinate must be [x, y], got {coord!r}")
        try:
            return self._map().to_screen(float(coord[0]), float(coord[1]))
        except ValueError as exc:
            raise ComputerError(str(exc)) from exc

    def _move_if(self, inp: dict[str, Any]) -> None:
        if inp.get("coordinate") is not None:
            self.io.move_to(*self._point(inp["coordinate"]))

    def _with_modifiers(self, text: str | None, action: Any) -> None:
        try:
            mods = keys.parse_modifiers(text)
        except keys.KeySpecError as exc:
            raise ComputerError(str(exc)) from exc
        for vk in mods:
            self.io.key_event(vk, up=False)
        try:
            action()
        finally:
            for vk in reversed(mods):
                self.io.key_event(vk, up=True)

    def _press(self, stroke: keys.KeyStroke) -> None:
        mods = list(stroke.modifiers)
        vk, ext = stroke.vk, stroke.extended
        if stroke.char is not None:
            found = self.io.vk_for_char(stroke.char)
            if found is None:
                if mods:
                    raise ComputerError(f"cannot press {stroke.char!r} with modifiers")
                self.io.type_unicode(stroke.char)
                return
            vk, shift = found
            if shift and 0x10 not in mods:
                mods.append(0x10)
        for m in mods:
            self.io.key_event(m, up=False)
        try:
            if vk is not None:
                self.io.key_event(vk, up=False, extended=ext)
                self.io.key_event(vk, up=True, extended=ext)
        finally:
            for m in reversed(mods):
                self.io.key_event(m, up=True)

    def _settle_then(self, result: ActionResult) -> ActionResult:
        time.sleep(self._settle)
        return result

    # ------------------------------------------------------------------ dispatch

    def execute(self, name: str, inp: dict[str, Any]) -> ActionResult:
        if name not in MEMBERS:
            raise ComputerError(f"unknown computer action {name!r}")
        handler = getattr(self, f"_do_{name}")
        result: ActionResult = handler(inp)
        return result

    def release_all(self) -> None:
        """Let go of anything still held (called when a task ends or is cancelled)."""
        for vk in list(self._held):
            self.io.key_event(vk, up=True)
        self._held.clear()
        for b in ("left", "right", "middle"):
            with contextlib.suppress(OSError):
                self.io.button(b, down=False)

    def _do_screenshot(self, inp: dict[str, Any]) -> ActionResult:
        png = self.screen.screenshot()
        w, h = self._map().shot_size
        return ActionResult(f"Screenshot {w}x{h}", png)

    def _do_zoom(self, inp: dict[str, Any]) -> ActionResult:
        region = inp.get("region")
        if not isinstance(region, list | tuple) or len(region) != 4:
            raise ComputerError("region must be [x0, y0, x1, y1]")
        try:
            png = self.screen.zoom(tuple(float(v) for v in region))  # type: ignore[arg-type]
        except ValueError as exc:
            raise ComputerError(str(exc)) from exc
        return ActionResult("Zoomed", png)

    def _click(self, inp: dict[str, Any], button: str, count: int) -> ActionResult:
        self._move_if(inp)
        self._with_modifiers(inp.get("text"), lambda: self.io.click(button, count))
        return self._settle_then(ActionResult())

    def _do_left_click(self, inp: dict[str, Any]) -> ActionResult:
        return self._click(inp, "left", 1)

    def _do_right_click(self, inp: dict[str, Any]) -> ActionResult:
        return self._click(inp, "right", 1)

    def _do_middle_click(self, inp: dict[str, Any]) -> ActionResult:
        return self._click(inp, "middle", 1)

    def _do_double_click(self, inp: dict[str, Any]) -> ActionResult:
        return self._click(inp, "left", 2)

    def _do_triple_click(self, inp: dict[str, Any]) -> ActionResult:
        return self._click(inp, "left", 3)

    def _do_left_click_drag(self, inp: dict[str, Any]) -> ActionResult:
        start = self._point(inp.get("start_coordinate"))
        end = self._point(inp.get("coordinate"))

        def drag() -> None:
            self.io.move_to(*start)
            self.io.button("left", down=True)
            try:
                steps = 10
                for i in range(1, steps + 1):
                    x = start[0] + (end[0] - start[0]) * i // steps
                    y = start[1] + (end[1] - start[1]) * i // steps
                    self.io.move_to(x, y)
                    time.sleep(0.01)
            finally:
                self.io.button("left", down=False)

        self._with_modifiers(inp.get("text"), drag)
        return self._settle_then(ActionResult())

    def _do_mouse_move(self, inp: dict[str, Any]) -> ActionResult:
        self.io.move_to(*self._point(inp.get("coordinate")))
        return ActionResult()

    def _do_left_mouse_down(self, inp: dict[str, Any]) -> ActionResult:
        self.io.button("left", down=True)
        return ActionResult()

    def _do_left_mouse_up(self, inp: dict[str, Any]) -> ActionResult:
        self.io.button("left", down=False)
        return self._settle_then(ActionResult())

    def _do_cursor_position(self, inp: dict[str, Any]) -> ActionResult:
        px, py = self.io.cursor_pos()
        smap = self._map()
        if not smap.contains(px, py):
            return ActionResult("The cursor is on another monitor, outside the screenshot.")
        x, y = smap.to_shot(px, py)
        return ActionResult(f"X={x}, Y={y}")

    def _do_scroll(self, inp: dict[str, Any]) -> ActionResult:
        direction = inp.get("scroll_direction")
        amount = inp.get("scroll_amount", 3)
        if direction not in {"up", "down", "left", "right"}:
            raise ComputerError("scroll_direction must be up, down, left or right")
        if not isinstance(amount, int) or not 0 < amount <= 50:
            raise ComputerError("scroll_amount must be an integer between 1 and 50")
        self._move_if(inp)
        clicks = amount if direction in {"up", "right"} else -amount
        horizontal = direction in {"left", "right"}
        self._with_modifiers(inp.get("text"), lambda: self.io.wheel(clicks, horizontal))
        return self._settle_then(ActionResult())

    def _do_type(self, inp: dict[str, Any]) -> ActionResult:
        text = inp.get("text")
        if not isinstance(text, str):
            raise ComputerError("text must be a string")
        for i in range(0, len(text), TYPE_CHUNK):
            chunk = text[i : i + TYPE_CHUNK]
            # Newlines and tabs as real key presses so editors and forms react normally.
            buf = ""
            for ch in chunk:
                if ch in "\r\n\t":
                    if buf:
                        self.io.type_unicode(buf)
                        buf = ""
                    if ch != "\r":
                        self._press(keys.parse_combo("Return" if ch == "\n" else "Tab"))
                else:
                    buf += ch
            if buf:
                self.io.type_unicode(buf)
            time.sleep(0.01)
        return self._settle_then(ActionResult())

    def _do_key(self, inp: dict[str, Any]) -> ActionResult:
        spec = inp.get("text")
        repeat = inp.get("repeat", 1)
        if not isinstance(spec, str):
            raise ComputerError("text must name a key, e.g. 'Return' or 'ctrl+s'")
        if not isinstance(repeat, int) or not 1 <= repeat <= 100:
            raise ComputerError("repeat must be between 1 and 100")
        try:
            stroke = keys.parse_combo(spec)
        except keys.KeySpecError as exc:
            raise ComputerError(str(exc)) from exc
        for _ in range(repeat):
            self._press(stroke)
            if repeat > 1:
                time.sleep(0.02)
        return self._settle_then(ActionResult())

    def _do_hold_key(self, inp: dict[str, Any]) -> ActionResult:
        spec = inp.get("text")
        duration = inp.get("duration")
        if not isinstance(spec, str):
            raise ComputerError("text must name a key")
        if not isinstance(duration, int | float) or not 0 < duration <= MAX_WAIT_S:
            raise ComputerError(f"duration must be between 0 and {MAX_WAIT_S:.0f} seconds")
        try:
            stroke = keys.parse_combo(spec)
        except keys.KeySpecError as exc:
            raise ComputerError(str(exc)) from exc
        held = [*stroke.modifiers, *([stroke.vk] if stroke.vk is not None else [])]
        for vk in held:
            self.io.key_event(vk, up=False, extended=stroke.extended)
            self._held.add(vk)
        try:
            time.sleep(float(duration))
        finally:
            for vk in reversed(held):
                self.io.key_event(vk, up=True, extended=stroke.extended)
                self._held.discard(vk)
        return ActionResult()

    def _do_wait(self, inp: dict[str, Any]) -> ActionResult:
        duration = inp.get("duration", 1)
        if not isinstance(duration, int | float) or not 0 <= duration <= MAX_WAIT_S:
            raise ComputerError(f"duration must be between 0 and {MAX_WAIT_S:.0f} seconds")
        time.sleep(float(duration))
        return ActionResult()
