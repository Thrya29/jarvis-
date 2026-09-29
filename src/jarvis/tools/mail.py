"""Email drafting. JARVIS never sends email: it opens a draft for the user to review."""

from __future__ import annotations

import asyncio
import email.policy
import os
import re
from email.message import EmailMessage
from pathlib import Path
from typing import Any, ClassVar

from pydantic import Field, field_validator

from jarvis.safety.policy import Risk
from jarvis.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult

_EMAIL_RE = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")


class DraftEmailArgs(ToolArgs):
    to: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    subject: str
    body: str = Field(description="Plain-text body.")
    attachments: list[str] = Field(default_factory=list, description="Paths of files to attach.")

    @field_validator("to", "cc")
    @classmethod
    def _valid_addresses(cls, v: list[str]) -> list[str]:
        bad = [a for a in v if not _EMAIL_RE.match(a.strip())]
        if bad:
            raise ValueError(f"invalid address(es): {bad}")
        return [a.strip() for a in v]


def _outlook_draft(args: DraftEmailArgs, attachments: list[Path]) -> bool:
    """Open an Outlook compose window. Returns False if classic Outlook isn't available."""
    try:
        import pythoncom
        import win32com.client
    except ImportError:
        return False
    pythoncom.CoInitialize()
    try:
        try:
            outlook = win32com.client.Dispatch("Outlook.Application")
        except Exception:  # COM raises pywintypes.com_error when Outlook isn't installed
            return False
        mail = outlook.CreateItem(0)
        mail.To = "; ".join(args.to)
        mail.CC = "; ".join(args.cc)
        mail.Subject = args.subject
        mail.Body = args.body
        for a in attachments:
            mail.Attachments.Add(str(a))
        mail.Display(False)  # show for review; never .Send()
        return True
    finally:
        pythoncom.CoUninitialize()


def _eml_draft(args: DraftEmailArgs, attachments: list[Path], out: Path) -> Path:
    msg = EmailMessage(policy=email.policy.SMTP)
    msg["To"] = ", ".join(args.to)
    if args.cc:
        msg["Cc"] = ", ".join(args.cc)
    msg["Subject"] = args.subject
    msg["X-Unsent"] = "1"  # mail clients open this as an editable draft
    msg.set_content(args.body)
    for a in attachments:
        msg.add_attachment(
            a.read_bytes(), maintype="application", subtype="octet-stream", filename=a.name
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(msg.as_bytes())
    if hasattr(os, "startfile"):
        os.startfile(out)  # noqa: S606 - opens our own generated draft in the mail client
    return out


class DraftEmail(Tool[DraftEmailArgs]):
    name = "draft_email"
    description = (
        "Open an email draft (Outlook if installed, otherwise the default mail app) with "
        "recipients, subject, body and attachments for the user to review and send "
        "themselves. JARVIS cannot send email."
    )
    args_model: ClassVar[type[ToolArgs]] = DraftEmailArgs
    risk = Risk.WRITE

    def summarize(self, args: DraftEmailArgs) -> str:
        return f"Draft email to {', '.join(args.to) or '(no recipient)'}: {args.subject!r}"

    async def run(self, args: DraftEmailArgs, ctx: ToolContext) -> ToolResult:
        attachments = [ctx.guard.resolve(a) for a in args.attachments]
        missing = [str(a) for a in attachments if not a.is_file()]
        if missing:
            raise ToolError(f"attachment(s) not found: {missing}")
        if await asyncio.to_thread(_outlook_draft, args, attachments):
            return ToolResult("Opened an Outlook draft for the user to review and send.")
        safe = re.sub(r"[^\w\- ]", "", args.subject)[:60].strip() or "draft"
        out = await asyncio.to_thread(
            _eml_draft, args, attachments, ctx.work_dir / "drafts" / f"{safe}.eml"
        )
        return ToolResult(f"Saved and opened draft {out} in the default mail app for review.")


EMAIL_TOOLS: list[Tool[Any]] = [DraftEmail()]
