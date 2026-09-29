from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, ClassVar

import pytest

from jarvis.agent.factory import all_tools
from jarvis.llm.base import ToolCall
from jarvis.safety.policy import Risk
from jarvis.tools import files as files_mod
from jarvis.tools import mail as mail_mod
from jarvis.tools.base import Tool, ToolArgs, ToolContext, ToolRegistry, ToolResult
from jarvis.tools.web import _check_public, html_to_text
from tests.fakes import ScriptedApprover, make_ctx

REG = ToolRegistry(all_tools())


def run(ctx: ToolContext, name: str, **args: Any) -> tuple[bool, str]:
    out = asyncio.run(REG.execute(ToolCall("t1", name, args), ctx))
    return not out.is_error, out.content


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "docs"


@pytest.fixture
def approver() -> ScriptedApprover:
    return ScriptedApprover(approve=True)


@pytest.fixture
def ctx(root: Path, approver: ScriptedApprover) -> ToolContext:
    return make_ctx(root, approver)


def test_tool_schemas_are_valid_objects() -> None:
    names = set()
    for spec in REG.specs():
        assert spec.input_schema["type"] == "object"
        assert spec.description
        names.add(spec.name)
    assert {"write_file", "create_excel", "draft_email", "run_shell", "update_plan"} <= names


def test_write_then_read(ctx: ToolContext, root: Path, approver: ScriptedApprover) -> None:
    ok, msg = run(ctx, "write_file", path="notes/a.txt", content="hello")
    assert ok, msg
    assert (root / "notes" / "a.txt").read_text(encoding="utf-8") == "hello"
    assert approver.requests == []  # new file inside root: auto-approved
    ok, msg = run(ctx, "read_file", path="notes/a.txt")
    assert ok and "hello" in msg and msg.startswith("<untrusted_content")


def test_overwrite_requires_flag_and_approval(
    ctx: ToolContext, root: Path, approver: ScriptedApprover
) -> None:
    run(ctx, "write_file", path="a.txt", content="v1")
    ok, msg = run(ctx, "write_file", path="a.txt", content="v2")
    assert not ok and "already exists" in msg
    ok, _ = run(ctx, "write_file", path="a.txt", content="v2", overwrite=True)
    assert ok
    assert approver.requests[-1].risk is Risk.DESTRUCTIVE
    assert (root / "a.txt").read_text(encoding="utf-8") == "v2"


def test_declined_action_is_not_performed(root: Path) -> None:
    ctx = make_ctx(root, ScriptedApprover(approve=False))
    (root / "keep.txt").write_text("x", encoding="utf-8")
    ok, msg = run(ctx, "delete_path", path="keep.txt")
    assert not ok and "declined" in msg and "not now" in msg
    assert (root / "keep.txt").exists()


def test_outside_root_is_refused(ctx: ToolContext, tmp_path: Path) -> None:
    ok, msg = run(ctx, "write_file", path=str(tmp_path / "evil.txt"), content="x")
    assert not ok and "outside the allowed folders" in msg
    assert not (tmp_path / "evil.txt").exists()


def test_invalid_input_reported(ctx: ToolContext) -> None:
    ok, msg = run(ctx, "write_file", path="a.txt")  # missing content
    assert not ok
    assert "INVALID_INPUT" in json.loads(msg)


def test_unknown_tool(ctx: ToolContext) -> None:
    ok, msg = run(ctx, "format_disk")
    assert not ok and "Unknown tool" in msg


def test_move_never_overwrites(ctx: ToolContext, root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "a.txt").write_text("a", encoding="utf-8")
    (root / "b.txt").write_text("b", encoding="utf-8")
    ok, msg = run(ctx, "move_path", source="a.txt", destination="b.txt")
    assert not ok and "already exists" in msg
    (root / "folder").mkdir()
    ok, _ = run(ctx, "move_path", source="a.txt", destination="folder")
    assert ok and (root / "folder" / "a.txt").exists() and not (root / "a.txt").exists()


def test_list_and_search(ctx: ToolContext, root: Path) -> None:
    (root / "p" / "q").mkdir(parents=True)
    (root / "p" / "report.md").write_text("quarterly revenue", encoding="utf-8")
    (root / "p" / "q" / "other.md").write_text("nothing", encoding="utf-8")
    ok, msg = run(ctx, "list_dir", path="p", recursive=True)
    assert ok and "report.md" in msg and "other.md" in msg
    ok, msg = run(ctx, "search_files", root="p", name_pattern="*.md", contains="REVENUE")
    assert ok and "report.md" in msg and "other.md" not in msg


def test_delete_goes_to_recycle_bin(
    ctx: ToolContext, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trashed: list[str] = []

    def fake_trash(p: str) -> None:
        trashed.append(p)
        Path(p).unlink()

    monkeypatch.setattr(files_mod, "send2trash", fake_trash)
    root.mkdir(parents=True, exist_ok=True)
    (root / "old.txt").write_text("x", encoding="utf-8")
    ok, _ = run(ctx, "delete_path", path="old.txt")
    assert ok and trashed == [str((root / "old.txt").resolve())]
    ok, msg = run(ctx, "delete_path", path=str(root))
    assert not ok and "root" in msg


def test_excel_and_word_roundtrip(ctx: ToolContext, root: Path) -> None:
    ok, msg = run(
        ctx,
        "create_excel",
        path="out/tracker.xlsx",
        sheets=[
            {
                "name": "Tasks",
                "headers": ["Task", "Owner", "Done"],
                "rows": [["Write spec", "Asha", False], ["Review", "Ravi", True]],
            }
        ],
    )
    assert ok, msg
    assert "Tasks: 2 rows" in msg
    ok, msg = run(ctx, "read_file", path="out/tracker.xlsx")
    assert ok and "Write spec" in msg and "Ravi" in msg

    ok, msg = run(
        ctx,
        "create_word_document",
        path="out/summary.docx",
        title="Summary",
        sections=[
            {"heading": "Findings", "paragraphs": ["All good."], "bullets": ["one", "two"]},
            {"table": [["A", "B"], ["1", "2"]]},
        ],
    )
    assert ok, msg
    ok, msg = run(ctx, "read_file", path="out/summary.docx")
    assert ok and "All good." in msg and "1 | 2" in msg


def test_excel_rejects_bad_sheet_name(ctx: ToolContext) -> None:
    ok, msg = run(ctx, "create_excel", path="x.xlsx", sheets=[{"name": "a/b", "headers": ["h"]}])
    assert not ok and "INVALID_INPUT" in msg


def test_run_python_scrubs_secrets(
    ctx: ToolContext, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-leak")
    code = "import os\nprint('KEY=' + str(os.environ.get('ANTHROPIC_API_KEY')))\nprint(2+2)"
    ok, msg = run(ctx, "run_python", code=code)
    assert ok, msg
    assert "KEY=None" in msg and "4" in msg and "exit code 0" in msg


def test_run_python_timeout_kills(ctx: ToolContext) -> None:
    ok, msg = run(ctx, "run_python", code="import time\ntime.sleep(30)", timeout_s=1)
    assert ok and "Timed out" in msg


def test_run_shell(ctx: ToolContext, approver: ScriptedApprover) -> None:
    ok, msg = run(ctx, "run_shell", command="Write-Output ('a' + 'b')")
    assert ok and "ab" in msg
    assert approver.requests[-1].risk is Risk.EXECUTE


def test_email_draft_fallback(
    ctx: ToolContext, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mail_mod, "_outlook_draft", lambda *a: False)
    opened: list[Path] = []
    monkeypatch.setattr(mail_mod.os, "startfile", opened.append, raising=False)
    root.mkdir(parents=True, exist_ok=True)
    (root / "r.txt").write_text("report", encoding="utf-8")
    ok, msg = run(
        ctx,
        "draft_email",
        to=["a@example.com"],
        subject="Results",
        body="Hi",
        attachments=["r.txt"],
    )
    assert ok, msg
    eml = opened[0].read_text(encoding="utf-8")
    assert "X-Unsent: 1" in eml and "Subject: Results" in eml and "r.txt" in eml


def test_email_rejects_bad_address(ctx: ToolContext) -> None:
    ok, msg = run(ctx, "draft_email", to=["not-an-email"], subject="x", body="y")
    assert not ok and "INVALID_INPUT" in msg


def test_html_to_text() -> None:
    title, body = html_to_text(
        "<html><head><title>T</title><script>evil()</script></head>"
        "<body><h1>Hello</h1><p>World &amp; more</p></body></html>"
    )
    assert title == "T"
    assert "evil" not in body and "Hello" in body and "World & more" in body


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:8765/v1/status", "http://localhost/", "http://10.0.0.1/", "file:///c:/"],
)
def test_fetch_refuses_non_public(url: str) -> None:
    from jarvis.tools.base import ToolError

    with pytest.raises(ToolError):
        _check_public(url)


class _Crashy(Tool[ToolArgs]):
    name = "crashy"
    description = "always crashes"
    args_model: ClassVar[type[ToolArgs]] = ToolArgs
    risk = Risk.READ

    async def run(self, args: ToolArgs, ctx: ToolContext) -> ToolResult:
        raise ZeroDivisionError("boom")


class _Slow(Tool[ToolArgs]):
    name = "slow"
    description = "sleeps"
    args_model: ClassVar[type[ToolArgs]] = ToolArgs
    risk = Risk.READ

    async def run(self, args: ToolArgs, ctx: ToolContext) -> ToolResult:
        await asyncio.sleep(10)
        return ToolResult("never")


def test_tool_crash_and_timeout_are_contained(root: Path) -> None:
    from jarvis.core.config import Settings

    settings = Settings(safety={"allowed_roots": [root]}, agent={"tool_timeout_s": 0.2})  # type: ignore[arg-type]
    ctx = make_ctx(root, settings=settings)
    reg = ToolRegistry([_Crashy(), _Slow()])
    out = asyncio.run(reg.execute(ToolCall("1", "crashy", {}), ctx))
    assert out.is_error and "ZeroDivisionError" in out.content
    out = asyncio.run(reg.execute(ToolCall("2", "slow", {}), ctx))
    assert out.is_error and "Timed out" in out.content


def test_audit_log_records_calls(ctx: ToolContext, root: Path) -> None:
    run(ctx, "write_file", path="a.txt", content="x")
    events = [
        json.loads(line)["event"]
        for line in (root.parent / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert events[-2:] == ["tool.call", "tool.done"]
