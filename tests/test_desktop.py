from __future__ import annotations

import asyncio
import base64
import io
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from jarvis.agent.factory import all_tools
from jarvis.agent.loop import HALT_TEXT, Agent, TaskStatus
from jarvis.core.config import AgentConfig, DesktopConfig, ScreenControl, Settings
from jarvis.desktop import keys
from jarvis.desktop.computer import ComputerController, ComputerError
from jarvis.desktop.killswitch import MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, hotkey_spec
from jarvis.desktop.screen import ScreenCapture, ScreenMap, scale_for
from jarvis.desktop.session import DesktopSession
from jarvis.desktop.uia import escape_sendkeys
from jarvis.llm.anthropic_provider import COMPUTER_TOOLSET, _tool_result
from jarvis.llm.base import ToolCall, ToolOutcome
from jarvis.server.session import TaskBoard
from jarvis.tools.base import ToolContext, ToolRegistry
from tests.fakes import ScriptedApprover, ScriptedProvider, calls, make_ctx, text

# ------------------------------------------------------------------ fakes


class FakeInput:
    def __init__(self) -> None:
        self.log: list[tuple[Any, ...]] = []
        self.pos = (0, 0)

    def move_to(self, x: int, y: int) -> None:
        self.pos = (x, y)
        self.log.append(("move", x, y))

    def cursor_pos(self) -> tuple[int, int]:
        return self.pos

    def button(self, name: str, down: bool) -> None:
        self.log.append(("button", name, down))

    def click(self, name: str, count: int) -> None:
        self.log.append(("click", name, count))

    def wheel(self, clicks: int, horizontal: bool) -> None:
        self.log.append(("wheel", clicks, horizontal))

    def key_event(self, vk: int, up: bool, extended: bool = False) -> None:
        self.log.append(("key", vk, "up" if up else "down"))

    def type_unicode(self, text: str) -> None:
        self.log.append(("type", text))

    def vk_for_char(self, ch: str) -> tuple[int, bool] | None:
        return {"!": (0x31, True), "/": (0xBF, False)}.get(ch)


class FakeScreen(ScreenCapture):
    """A 2732x1536 'monitor' (so screenshots are scaled by 0.5 at max_edge=1366)."""

    def __init__(self) -> None:
        super().__init__(monitor=1, max_edge=1366)
        self.grabs = 0

    def _grab(self) -> tuple[Image.Image, ScreenMap]:
        self.grabs += 1
        img = Image.new("RGB", (2732, 1536), (30, 30, 30))
        img.paste((255, 0, 0), (100, 100, 300, 300))
        return img, ScreenMap(0, 0, 2732, 1536, scale_for(2732, 1536, 1366))


class FakeUIA:
    def __init__(self, title: str = "Untitled - Notepad", password: bool = False) -> None:
        self.title = title
        self.password = password

    async def foreground_title(self) -> str:
        return self.title

    def is_blocked(self, title: str) -> bool:
        return "keepass" in title.lower()

    async def focused_is_password(self) -> bool:
        return self.password

    def close(self) -> None:
        pass


def desktop(
    mode: ScreenControl = ScreenControl.ASK, uia: FakeUIA | None = None
) -> tuple[DesktopSession, FakeInput]:
    inp = FakeInput()
    session = DesktopSession(
        DesktopConfig(screen_control=mode),
        uia or FakeUIA(),  # type: ignore[arg-type]
        ComputerController(FakeScreen(), inp, settle_s=0),
    )
    return session, inp


def png_size(data: bytes) -> tuple[int, int]:
    return Image.open(io.BytesIO(data)).size


# ------------------------------------------------------------------ keys


@pytest.mark.parametrize(
    ("spec", "mods", "vk"),
    [
        ("Return", (), 0x0D),
        ("ctrl+s", (0x11,), ord("S")),
        ("ctrl+shift+Tab", (0x11, 0x10), 0x09),
        ("alt+F4", (0x12,), 0x73),
        ("super", (0x5B,), None),
        ("Page_Down", (), 0x22),
    ],
)
def test_parse_combo(spec: str, mods: tuple[int, ...], vk: int | None) -> None:
    stroke = keys.parse_combo(spec)
    assert stroke.modifiers == mods and stroke.vk == vk


def test_parse_combo_errors() -> None:
    with pytest.raises(keys.KeySpecError):
        keys.parse_combo("banana+s")
    with pytest.raises(keys.KeySpecError):
        keys.parse_combo("NotAKey")


def test_hotkey_spec() -> None:
    mods, vk = hotkey_spec("ctrl+alt+j")
    assert mods == MOD_CONTROL | MOD_ALT | MOD_NOREPEAT and vk == ord("J")
    with pytest.raises(keys.KeySpecError):
        hotkey_spec("j")


def test_sendkeys_escape() -> None:
    assert escape_sendkeys("a{b}c") == "a{{}b{}}c"


# ------------------------------------------------------------------ screen


def test_screen_map_roundtrip() -> None:
    m = ScreenMap(left=-1920, top=0, width=2732, height=1536, scale=0.5)
    assert m.shot_size == (1366, 768)
    assert m.to_screen(683, 384) == (-1920 + 1366, 768)
    assert m.to_shot(-1920 + 1366, 768) == (683, 384)
    with pytest.raises(ValueError):
        m.to_screen(2000, 10)


def test_scale_never_upscales_and_caps_edge() -> None:
    assert scale_for(1280, 720, 1366) == 1.0
    assert scale_for(3840, 2160, 5000) == pytest.approx(2000 / 3840)


def test_screenshot_and_zoom() -> None:
    screen = FakeScreen()
    assert png_size(screen.screenshot()) == (1366, 768)
    # Region in screenshot pixels -> full-resolution crop (2x the size).
    assert png_size(screen.zoom((50, 50, 150, 150))) == (200, 200)
    with pytest.raises(ValueError):
        screen.zoom((10, 10, 5, 5))


# ------------------------------------------------------------------ controller


def test_click_maps_coordinates_and_modifiers() -> None:
    session, inp = desktop()
    assert session.computer is not None
    session.computer.execute("screenshot", {})
    session.computer.execute("left_click", {"coordinate": [100, 50], "text": "shift"})
    assert inp.log == [
        ("move", 200, 100),
        ("key", 0x10, "down"),
        ("click", "left", 1),
        ("key", 0x10, "up"),
    ]


def test_double_triple_right_and_scroll() -> None:
    session, inp = desktop()
    c = session.computer
    assert c is not None
    c.execute("double_click", {"coordinate": [10, 10]})
    c.execute("triple_click", {})
    c.execute("right_click", {})
    c.execute("scroll", {"coordinate": [10, 10], "scroll_direction": "down", "scroll_amount": 3})
    c.execute("scroll", {"scroll_direction": "left", "scroll_amount": 2})
    clicks = [e for e in inp.log if e[0] in {"click", "wheel"}]
    assert clicks == [
        ("click", "left", 2),
        ("click", "left", 3),
        ("click", "right", 1),
        ("wheel", -3, False),
        ("wheel", -2, True),
    ]


def test_type_uses_real_enter_and_tab() -> None:
    session, inp = desktop()
    assert session.computer is not None
    session.computer.execute("type", {"text": "a\tb\nc"})
    assert inp.log == [
        ("type", "a"),
        ("key", 0x09, "down"),
        ("key", 0x09, "up"),
        ("type", "b"),
        ("key", 0x0D, "down"),
        ("key", 0x0D, "up"),
        ("type", "c"),
    ]


def test_key_with_shifted_char_and_repeat() -> None:
    session, inp = desktop()
    assert session.computer is not None
    session.computer.execute("key", {"text": "!"})
    assert inp.log == [
        ("key", 0x10, "down"),
        ("key", 0x31, "down"),
        ("key", 0x31, "up"),
        ("key", 0x10, "up"),
    ]
    inp.log.clear()
    session.computer.execute("key", {"text": "Down", "repeat": 3})
    assert inp.log.count(("key", 0x28, "down")) == 3


def test_drag_releases_button() -> None:
    session, inp = desktop()
    assert session.computer is not None
    session.computer.execute(
        "left_click_drag", {"start_coordinate": [0, 0], "coordinate": [100, 100]}
    )
    assert ("button", "left", True) in inp.log
    assert inp.log[-1] == ("button", "left", False)
    assert inp.pos == (200, 200)


def test_cursor_position_in_screenshot_space() -> None:
    session, inp = desktop()
    assert session.computer is not None
    inp.pos = (400, 300)
    assert session.computer.execute("cursor_position", {}).text == "X=200, Y=150"


@pytest.mark.parametrize(
    ("name", "inp"),
    [
        ("left_click", {"coordinate": [99999, 1]}),
        ("left_click", {"coordinate": "10,10"}),
        ("scroll", {"scroll_direction": "sideways", "scroll_amount": 1}),
        ("key", {"text": "hyper+q"}),
        ("wait", {"duration": 999}),
        ("zoom", {"region": [1, 2]}),
        ("teleport", {}),
    ],
)
def test_controller_rejects_bad_input(name: str, inp: dict[str, Any]) -> None:
    session, _ = desktop()
    assert session.computer is not None
    with pytest.raises(ComputerError):
        session.computer.execute(name, inp)


def test_release_all_lets_go() -> None:
    session, inp = desktop()
    assert session.computer is not None
    session.computer._held.add(0x10)
    session.end_task("t1")
    assert ("key", 0x10, "up") in inp.log
    assert ("button", "left", False) in inp.log


# ------------------------------------------------------------------ registry + consent


def ctx_with(tmp_path: Path, session: DesktopSession, approve: bool = True) -> ToolContext:
    ctx = make_ctx(tmp_path / "docs", ScriptedApprover(approve=approve))
    ctx.desktop = session
    ctx.task_id = "task-1"
    return ctx


def run_call(ctx: ToolContext, name: str, inp: dict[str, Any]) -> ToolOutcome:
    reg = ToolRegistry(all_tools(desktop=True))
    return asyncio.run(reg.execute(ToolCall("c1", name, inp, toolset="computer"), ctx))


def test_consent_asked_once_per_task(tmp_path: Path) -> None:
    session, _ = desktop()
    ctx = ctx_with(tmp_path, session)
    out = run_call(ctx, "screenshot", {})
    assert not out.is_error and out.toolset == "computer"
    assert len(out.images) == 1 and png_size(out.images[0]) == (1366, 768)
    run_call(ctx, "left_click", {"coordinate": [1, 1]})
    approver = ctx.approver
    assert isinstance(approver, ScriptedApprover)
    assert [r.tool for r in approver.requests] == ["screen_control"]
    session.end_task("task-1")
    ctx.task_id = "task-2"
    run_call(ctx, "screenshot", {})
    assert len(approver.requests) == 2


def test_consent_declined(tmp_path: Path) -> None:
    session, inp = desktop()
    ctx = ctx_with(tmp_path, session, approve=False)
    out = run_call(ctx, "left_click", {"coordinate": [1, 1]})
    assert out.is_error and "declined" in out.content and out.toolset == "computer"
    assert inp.log == []
    run_call(ctx, "screenshot", {})  # not asked again in the same task
    approver = ctx.approver
    assert isinstance(approver, ScriptedApprover) and len(approver.requests) == 1


def test_deny_mode_and_allow_mode(tmp_path: Path) -> None:
    session, _ = desktop(ScreenControl.DENY)
    out = run_call(ctx_with(tmp_path, session), "screenshot", {})
    assert out.is_error and "disabled" in out.content
    session, _ = desktop(ScreenControl.ALLOW)
    ctx = ctx_with(tmp_path, session)
    assert not run_call(ctx, "screenshot", {}).is_error
    approver = ctx.approver
    assert isinstance(approver, ScriptedApprover) and approver.requests == []


def test_protected_window_is_refused(tmp_path: Path) -> None:
    session, inp = desktop(uia=FakeUIA(title="Passwords.kdbx - KeePass"))
    ctx = ctx_with(tmp_path, session)
    for name, args in (("screenshot", {}), ("type", {"text": "x"})):
        out = run_call(ctx, name, args)
        assert out.is_error and "protected" in out.content
    assert inp.log == []
    assert not run_call(ctx, "wait", {"duration": 0}).is_error


def test_password_field_typing_refused(tmp_path: Path) -> None:
    session, inp = desktop(uia=FakeUIA(password=True))
    out = run_call(ctx_with(tmp_path, session), "type", {"text": "hunter2"})
    assert out.is_error and "password" in out.content
    assert inp.log == []


def test_typed_text_not_written_to_audit(tmp_path: Path) -> None:
    session, _ = desktop()
    ctx = ctx_with(tmp_path, session)
    run_call(ctx, "type", {"text": "my private note"})
    audit = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "my private note" not in audit and "<15 chars>" in audit


def test_desktop_tools_need_consent(tmp_path: Path) -> None:
    session, _ = desktop(ScreenControl.ASK)
    ctx = ctx_with(tmp_path, session, approve=False)
    reg = ToolRegistry(all_tools(desktop=True))
    out = asyncio.run(reg.execute(ToolCall("1", "list_windows", {}), ctx))
    assert out.is_error and "declined" in out.content


# ------------------------------------------------------------------ agent loop


async def test_batch_halts_after_failed_computer_action(tmp_path: Path) -> None:
    session, inp = desktop()
    provider = ScriptedProvider(
        [
            _computer_calls(
                ("left_click", {"coordinate": [99999, 5]}),  # out of bounds -> fails
                ("type", {"text": "should not type"}),
                ("screenshot", {}),
            ),
            text("done"),
        ],
        supports_computer_use=True,
    )
    ctx = make_ctx(tmp_path / "docs", ScriptedApprover())
    ctx.desktop = session
    agent = Agent(provider, ToolRegistry(all_tools(desktop=True)), ctx, AgentConfig())
    assert provider.computer_use is True
    assert "Operating the screen" in provider.system
    result = await agent.run("click something")
    assert result.status is TaskStatus.COMPLETED
    first, second, third = provider.conversation.tool_results()
    assert first.is_error and "outside" in first.content
    assert second.content == HALT_TEXT and third.content == HALT_TEXT
    assert all(r.toolset == "computer" for r in (first, second, third))
    assert ("type", "should not type") not in inp.log


async def test_no_pixel_control_for_text_only_models(tmp_path: Path) -> None:
    session, _ = desktop()
    provider = ScriptedProvider([text("ok")], supports_computer_use=False)
    ctx = make_ctx(tmp_path / "docs")
    ctx.desktop = session
    Agent(provider, ToolRegistry(all_tools(desktop=True)), ctx, AgentConfig())
    assert provider.computer_use is False
    assert "not available with the current model" in provider.system
    assert "inspect_window" in {t.name for t in provider.tools}


def _computer_calls(*items: tuple[str, dict[str, Any]]):  # type: ignore[no-untyped-def]
    turn = calls(*items)
    return type(turn)(
        turn.text,
        [ToolCall(c.id, c.name, c.input, toolset="computer") for c in turn.tool_calls],
        turn.stop,
        turn.usage,
    )


# ------------------------------------------------------------------ wire format


def test_anthropic_tool_result_with_image_and_toolset() -> None:
    block = _tool_result(
        ToolOutcome(
            "toolu_1", "screenshot", "Screenshot 1366x768", images=(b"PNG",), toolset="computer"
        )
    )
    assert block["toolset_name"] == "computer"
    assert block["content"][0]["type"] == "image"
    assert base64.b64decode(block["content"][0]["source"]["data"]) == b"PNG"
    assert block["content"][1] == {"type": "text", "text": "Screenshot 1366x768"}
    plain = _tool_result(ToolOutcome("toolu_2", "write_file", "ok"))
    assert "toolset_name" not in plain and plain["content"] == "ok"


def test_anthropic_declares_toolset_when_enabled() -> None:
    from jarvis.core.config import AnthropicConfig
    from jarvis.llm.anthropic_provider import AnthropicProvider

    conv = AnthropicProvider(AnthropicConfig(), "k").new_conversation("s", [], computer_use=True)
    assert {"type": COMPUTER_TOOLSET} in conv._tools


# ------------------------------------------------------------------ kill switch plumbing


async def test_task_board_cancel_all() -> None:
    board = TaskBoard()

    async def forever() -> None:
        await asyncio.sleep(3600)

    t1 = asyncio.create_task(forever())
    board.track(t1)
    await asyncio.sleep(0)
    assert board.cancel_all() == 1
    with pytest.raises(asyncio.CancelledError):
        await t1
    assert board.tasks == set()


def test_settings_defaults() -> None:
    s = Settings()
    assert s.desktop.screen_control is ScreenControl.ASK
    assert any("keepass" in p for p in s.desktop.blocked_windows)


def test_cursor_on_other_monitor_is_reported() -> None:
    session, inp = desktop()
    assert session.computer is not None
    inp.pos = (-500, 300)  # a monitor to the left of the controlled one
    assert "another monitor" in session.computer.execute("cursor_position", {}).text


def test_screen_map_contains() -> None:
    m = ScreenMap(0, 0, 1920, 1080, 0.7)
    assert m.contains(0, 0) and m.contains(1919, 1079)
    assert not m.contains(-1, 5) and not m.contains(1920, 5)
