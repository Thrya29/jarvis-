"""Desktop tools that work with any model: windows, UI Automation, launching apps.

Claude additionally gets pixel-level control through the computer-use toolset
(see :mod:`jarvis.desktop.computer`); these structured tools are more reliable for
standard Windows apps and are the only screen tools text-only local models can use.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import Field

from jarvis.desktop.uia import UIAError
from jarvis.safety.policy import Risk
from jarvis.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult


def _uia(ctx: ToolContext) -> Any:
    if ctx.desktop is None:
        raise ToolError("desktop control is not available in this session")
    return ctx.desktop.uia


class NoArgs(ToolArgs):
    pass


class ListWindows(Tool[NoArgs]):
    name = "list_windows"
    description = "List open application windows (title, program, process id)."
    args_model: ClassVar[type[ToolArgs]] = NoArgs
    risk = Risk.READ
    requires_desktop = True

    async def run(self, args: NoArgs, ctx: ToolContext) -> ToolResult:
        uia = _uia(ctx)
        wins = await uia.list_windows()
        lines = []
        for w in wins:
            mark = "[protected] " if uia.is_blocked(w.title) else ""
            lines.append(f"- {mark}{w.title}  ({w.process}, pid {w.pid})")
        return ToolResult(
            "\n".join(lines) or "No windows open.", untrusted=True, source="window list"
        )


class FocusWindowArgs(ToolArgs):
    title: str = Field(description="Window title or part of it (case-insensitive).")


class FocusWindow(Tool[FocusWindowArgs]):
    name = "focus_window"
    description = "Bring a window to the front by (part of) its title."
    args_model: ClassVar[type[ToolArgs]] = FocusWindowArgs
    risk = Risk.WRITE
    requires_desktop = True

    async def run(self, args: FocusWindowArgs, ctx: ToolContext) -> ToolResult:
        try:
            title = await _uia(ctx).focus_window(args.title)
        except UIAError as exc:
            raise ToolError(str(exc)) from exc
        return ToolResult(f"Focused '{title}'.")


class InspectWindowArgs(ToolArgs):
    title: str | None = Field(
        default=None, description="Window title (or part). Default: the foreground window."
    )
    max_depth: int = Field(default=8, ge=1, le=25)


class InspectWindow(Tool[InspectWindowArgs]):
    name = "inspect_window"
    description = (
        "Read a window's UI as a tree of controls (buttons, fields, menus, list items) with "
        "numeric ids, names, values and screen rectangles. Use the ids with click_element "
        "and set_element_text. Ids are valid until the next inspect_window call."
    )
    args_model: ClassVar[type[ToolArgs]] = InspectWindowArgs
    risk = Risk.READ
    requires_desktop = True

    async def run(self, args: InspectWindowArgs, ctx: ToolContext) -> ToolResult:
        try:
            tree = await _uia(ctx).inspect(args.title, args.max_depth)
        except UIAError as exc:
            raise ToolError(str(exc)) from exc
        return ToolResult(tree, untrusted=True, source="UI Automation tree")


class ClickElementArgs(ToolArgs):
    element_id: int = Field(ge=0)
    double: bool = False


class ClickElement(Tool[ClickElementArgs]):
    name = "click_element"
    description = (
        "Activate a control from the last inspect_window: invokes buttons/menu items, "
        "toggles checkboxes, selects list items; falls back to a real mouse click."
    )
    args_model: ClassVar[type[ToolArgs]] = ClickElementArgs
    risk = Risk.WRITE
    requires_desktop = True

    async def run(self, args: ClickElementArgs, ctx: ToolContext) -> ToolResult:
        try:
            return ToolResult(await _uia(ctx).click(args.element_id, args.double))
        except UIAError as exc:
            raise ToolError(str(exc)) from exc


class SetElementTextArgs(ToolArgs):
    element_id: int = Field(ge=0)
    text: str


class SetElementText(Tool[SetElementTextArgs]):
    name = "set_element_text"
    description = (
        "Replace the text of an edit field or document from the last inspect_window. "
        "Refuses password fields."
    )
    args_model: ClassVar[type[ToolArgs]] = SetElementTextArgs
    risk = Risk.WRITE
    requires_desktop = True

    def summarize(self, args: SetElementTextArgs) -> str:
        return f"Set element {args.element_id} text ({len(args.text)} chars)"

    async def run(self, args: SetElementTextArgs, ctx: ToolContext) -> ToolResult:
        try:
            return ToolResult(await _uia(ctx).set_text(args.element_id, args.text))
        except UIAError as exc:
            raise ToolError(str(exc)) from exc


class LaunchAppArgs(ToolArgs):
    name: str = Field(description="Installed app name as in the Start menu, e.g. 'Notepad'.")


class LaunchApp(Tool[LaunchAppArgs]):
    name = "launch_app"
    description = (
        "Start an installed application by its Start-menu name (desktop and Store apps). "
        "After launching, use list_windows / inspect_window or a screenshot to continue."
    )
    args_model: ClassVar[type[ToolArgs]] = LaunchAppArgs
    risk = Risk.WRITE
    requires_desktop = True

    def summarize(self, args: LaunchAppArgs) -> str:
        return f"Launch {args.name}"

    async def run(self, args: LaunchAppArgs, ctx: ToolContext) -> ToolResult:
        try:
            return ToolResult(await _uia(ctx).launch(args.name))
        except UIAError as exc:
            raise ToolError(str(exc)) from exc


DESKTOP_TOOLS: list[Tool[Any]] = [
    ListWindows(),
    FocusWindow(),
    InspectWindow(),
    ClickElement(),
    SetElementText(),
    LaunchApp(),
]
