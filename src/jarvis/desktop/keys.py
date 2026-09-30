"""Key-name parsing for combos like ``ctrl+shift+s``, ``alt+Tab``, ``Return``, ``F5``.

Names follow the xdotool/X11 spelling the computer-use model emits, plus common aliases.
"""

from __future__ import annotations

from dataclasses import dataclass

MODIFIERS: dict[str, int] = {
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "option": 0x12,
    "super": 0x5B, "win": 0x5B, "windows": 0x5B, "cmd": 0x5B, "meta": 0x5B,
}  # fmt: skip

# name -> (virtual key, extended)
NAMED: dict[str, tuple[int, bool]] = {
    "return": (0x0D, False), "enter": (0x0D, False), "kp_enter": (0x0D, True),
    "tab": (0x09, False), "escape": (0x1B, False), "esc": (0x1B, False),
    "backspace": (0x08, False), "space": (0x20, False),
    "delete": (0x2E, True), "del": (0x2E, True), "insert": (0x2D, True),
    "home": (0x24, True), "end": (0x23, True),
    "page_up": (0x21, True), "pageup": (0x21, True), "prior": (0x21, True),
    "page_down": (0x22, True), "pagedown": (0x22, True), "next": (0x22, True),
    "up": (0x26, True), "down": (0x28, True), "left": (0x25, True), "right": (0x27, True),
    "caps_lock": (0x14, False), "capslock": (0x14, False),
    "print": (0x2C, True), "printscreen": (0x2C, True), "menu": (0x5D, True),
    "volumeup": (0xAF, True), "volumedown": (0xAE, True), "volumemute": (0xAD, True),
    "xf86audioraisevolume": (0xAF, True), "xf86audiolowervolume": (0xAE, True),
    "xf86audiomute": (0xAD, True), "xf86audioplay": (0xB3, True),
    "minus": (0xBD, False), "plus": (0xBB, False), "equal": (0xBB, False),
    "comma": (0xBC, False), "period": (0xBE, False), "slash": (0xBF, False),
    "semicolon": (0xBA, False), "apostrophe": (0xDE, False), "grave": (0xC0, False),
    "bracketleft": (0xDB, False), "bracketright": (0xDD, False), "backslash": (0xDC, False),
}  # fmt: skip
NAMED.update({f"f{i}": (0x6F + i, False) for i in range(1, 25)})


@dataclass(frozen=True)
class KeyStroke:
    modifiers: tuple[int, ...]
    vk: int | None  # None for a modifier-only combo ("shift")
    extended: bool = False
    char: str | None = None  # a character to resolve against the active layout


class KeySpecError(ValueError):
    pass


def parse_combo(spec: str) -> KeyStroke:
    parts = [p for p in spec.replace(" ", "").split("+") if p] if spec != "+" else ["+"]
    if spec.endswith("++"):
        parts.append("+")
    if not parts:
        raise KeySpecError("empty key")
    mods: list[int] = []
    for p in parts[:-1]:
        vk = MODIFIERS.get(p.lower())
        if vk is None:
            raise KeySpecError(f"{p!r} is not a modifier in {spec!r}")
        mods.append(vk)
    last = parts[-1]
    low = last.lower()
    if low in MODIFIERS:
        return KeyStroke((*mods, MODIFIERS[low]), None)
    if low in NAMED:
        vk, ext = NAMED[low]
        return KeyStroke(tuple(mods), vk, ext)
    if len(last) == 1:
        if last.isalnum() and last.isascii():
            return KeyStroke(tuple(mods), ord(last.upper()))
        return KeyStroke(tuple(mods), None, char=last)
    raise KeySpecError(f"unknown key {last!r}")


def parse_modifiers(text: str | None) -> tuple[int, ...]:
    if not text:
        return ()
    out = []
    for p in text.replace(" ", "").split("+"):
        vk = MODIFIERS.get(p.lower())
        if vk is None:
            raise KeySpecError(f"{p!r} is not a modifier key")
        out.append(vk)
    return tuple(out)
