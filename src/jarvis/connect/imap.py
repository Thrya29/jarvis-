"""Plain IMAP/SMTP mail for providers without Microsoft/Google sign-in (Zoho, company servers).

Uses an app password the user creates with their provider. All socket work runs in a
worker thread; each call opens a short-lived TLS connection, so there's no idle socket.
"""

from __future__ import annotations

import asyncio
import contextlib
import email
import email.policy
import email.utils
import imaplib
import re
import smtplib
import ssl
import time
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any

from jarvis.connect.services import (
    MAX_BODY_CHARS,
    Draft,
    Mail,
    MailSummary,
    ServiceError,
    html_text,
)

TIMEOUT_S = 30


@dataclass(frozen=True)
class ImapSettings:
    email: str
    username: str
    imap_host: str
    imap_port: int = 993
    smtp_host: str = ""
    smtp_port: int = 465  # 465 = implicit TLS; 587 = STARTTLS

    def to_dict(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "username": self.username,
            "imap_host": self.imap_host,
            "imap_port": self.imap_port,
            "smtp_host": self.smtp_host,
            "smtp_port": self.smtp_port,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ImapSettings:
        return cls(
            d["email"],
            d.get("username") or d["email"],
            d["imap_host"],
            int(d.get("imap_port", 993)),
            d.get("smtp_host", ""),
            int(d.get("smtp_port", 465)),
        )


_LIST_RE = re.compile(rb'\((?P<flags>[^)]*)\) "(?P<sep>[^"]*)" (?P<name>.+)$')


def _unquote(name: bytes) -> str:
    text = name.decode("utf-8", errors="replace").strip()
    return text[1:-1].replace('\\"', '"') if text.startswith('"') else text


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _body(msg: email.message.Message) -> str:
    plain = msg.get_body(preferencelist=("plain",)) if hasattr(msg, "get_body") else None
    part = plain or (msg.get_body(preferencelist=("html",)) if hasattr(msg, "get_body") else None)
    if part is None:
        return ""
    try:
        text = str(part.get_content())
    except (LookupError, ValueError):
        payload = part.get_payload(decode=True)
        text = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else ""
    return text if part is plain else html_text(text)


def _addresses(value: str | None) -> list[str]:
    return [a for _, a in email.utils.getaddresses([value or ""]) if a]


class ImapService:
    label = "Email (IMAP)"

    def __init__(self, settings: ImapSettings, password: str) -> None:
        self.s = settings
        self._password = password

    # ---------------------------------------------------------------- plumbing
    def _imap(self) -> imaplib.IMAP4_SSL:
        try:
            conn = imaplib.IMAP4_SSL(
                self.s.imap_host,
                self.s.imap_port,
                ssl_context=ssl.create_default_context(),
                timeout=TIMEOUT_S,
            )
        except (OSError, imaplib.IMAP4.error) as exc:
            raise ServiceError(f"could not reach {self.s.imap_host}: {exc}") from exc
        try:
            conn.login(self.s.username, self._password)
        except imaplib.IMAP4.error as exc:
            with contextlib.suppress(Exception):
                conn.logout()
            raise ServiceError(
                "the mail server rejected the username or app password. Many providers need "
                "an app password rather than your normal password."
            ) from exc
        return conn

    @staticmethod
    def _ok(result: tuple[str, Any], what: str) -> Any:
        typ, data = result
        if typ != "OK":
            raise ServiceError(f"mail server error during {what}: {data!r}"[:300])
        return data

    def _special(self, conn: imaplib.IMAP4_SSL, flag: bytes, fallback: str) -> str:
        typ, rows = conn.list()
        names: list[tuple[str, str]] = []
        for row in rows if typ == "OK" else []:
            m = _LIST_RE.match(row) if isinstance(row, bytes) else None
            if not m:
                continue
            name = _unquote(m.group("name"))
            names.append((name, m.group("sep").decode() or "/"))
            if flag.lower() in m.group("flags").lower():
                return name
        for name, sep in names:  # servers without SPECIAL-USE: match by leaf name
            if name.split(sep)[-1].lower() in (fallback.lower(), f"{fallback.lower()} items"):
                return name
        return fallback

    def _fetch(self, conn: imaplib.IMAP4_SSL, uid: str, what: str) -> bytes:
        data = self._ok(conn.uid("FETCH", uid, what), "fetch")
        for item in data:
            if isinstance(item, tuple) and len(item) == 2:
                return bytes(item[1])
        raise ServiceError("message not found (it may have been moved or deleted)")

    @staticmethod
    def _check_uid(uid: str) -> str:
        if not uid.isdigit():
            raise ServiceError("invalid message id")
        return uid

    # ---------------------------------------------------------------- sync bodies
    def _verify(self) -> str:
        conn = self._imap()
        with contextlib.suppress(Exception):
            conn.logout()
        return self.s.email

    def _search(self, query: str, limit: int) -> list[MailSummary]:
        conn = self._imap()
        try:
            self._ok(conn.select("INBOX", readonly=True), "select")
            q = query.strip()
            if not q:
                data = self._ok(conn.uid("SEARCH", "ALL"), "search")
            elif q.isascii():
                data = self._ok(conn.uid("SEARCH", "TEXT", _quote(q)), "search")
            else:
                conn.literal = q.encode("utf-8")  # type: ignore[assignment]
                data = self._ok(conn.uid("SEARCH", "CHARSET", "UTF-8", "TEXT"), "search")
            uids = (data[0] or b"").split()[-limit:][::-1]
            out = []
            for uid in uids:
                raw = self._fetch(
                    conn, uid.decode(), "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])"
                )
                msg = email.message_from_bytes(raw, policy=email.policy.default)
                out.append(
                    MailSummary(
                        uid.decode(),
                        str(msg.get("From", "")),
                        str(msg.get("Subject", "(no subject)")),
                        str(msg.get("Date", "")),
                        "",
                        False,
                    )
                )
            return out
        finally:
            with contextlib.suppress(Exception):
                conn.logout()

    def _read(self, uid: str) -> Mail:
        conn = self._imap()
        try:
            self._ok(conn.select("INBOX", readonly=True), "select")
            raw = self._fetch(conn, self._check_uid(uid), "(BODY.PEEK[])")
        finally:
            with contextlib.suppress(Exception):
                conn.logout()
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        body = _body(msg)
        if len(body) > MAX_BODY_CHARS:
            body = body[:MAX_BODY_CHARS] + "\n[... truncated ...]"
        attachments = [
            str(p.get_filename()) for p in msg.walk() if p.get_content_disposition() == "attachment"
        ]
        return Mail(
            uid,
            str(msg.get("From", "")),
            _addresses(msg.get("To")),
            _addresses(msg.get("Cc")),
            str(msg.get("Subject", "")),
            str(msg.get("Date", "")),
            body,
            attachments,
        )

    def _draft(
        self, to: list[str], cc: list[str], subject: str, body: str, reply_to: str | None
    ) -> Draft:
        conn = self._imap()
        try:
            msg = EmailMessage(policy=email.policy.SMTP)
            if reply_to:
                self._ok(conn.select("INBOX", readonly=True), "select")
                raw = self._fetch(
                    conn,
                    self._check_uid(reply_to),
                    "(BODY.PEEK[HEADER.FIELDS (FROM REPLY-TO SUBJECT MESSAGE-ID)])",
                )
                orig = email.message_from_bytes(raw, policy=email.policy.default)
                to = to or _addresses(orig.get("Reply-To") or orig.get("From"))
                subject = subject or str(orig.get("Subject", ""))
                if not subject.lower().startswith("re:"):
                    subject = f"Re: {subject}"
                if orig.get("Message-ID"):
                    msg["In-Reply-To"] = str(orig["Message-ID"])
                    msg["References"] = str(orig["Message-ID"])
                conn.close()
            msg["From"] = self.s.email
            msg["To"] = ", ".join(to)
            if cc:
                msg["Cc"] = ", ".join(cc)
            msg["Subject"] = subject
            msg["Date"] = email.utils.formatdate(localtime=True)
            msg["Message-ID"] = email.utils.make_msgid()
            msg.set_content(body)
            drafts = self._special(conn, b"\\Drafts", "Drafts")
            data = self._ok(
                conn.append(
                    _quote(drafts),
                    "(\\Draft \\Seen)",
                    imaplib.Time2Internaldate(time.time()),
                    msg.as_bytes(),
                ),
                "save draft",
            )
            # Servers with UIDPLUS answer "[APPENDUID <validity> <uid>]".
            m = re.search(rb"APPENDUID \d+ (\d+)", data[0] or b"")
            uid = m.group(1).decode() if m else ""
            if not uid:
                self._ok(conn.select(_quote(drafts), readonly=True), "select drafts")
                found = self._ok(
                    conn.uid("SEARCH", "HEADER", "Message-ID", _quote(str(msg["Message-ID"]))),
                    "find draft",
                )
                uid = (found[0] or b"").split()[-1].decode() if found[0] else ""
            if not uid:
                raise ServiceError("the draft was saved but the server didn't return its id")
            return Draft(uid, to, subject)
        finally:
            with contextlib.suppress(Exception):
                conn.logout()

    def _load_draft(self, conn: imaplib.IMAP4_SSL, uid: str) -> tuple[str, EmailMessage]:
        drafts = self._special(conn, b"\\Drafts", "Drafts")
        self._ok(conn.select(_quote(drafts)), "select drafts")
        raw = self._fetch(conn, self._check_uid(uid), "(BODY.PEEK[])")
        msg = email.message_from_bytes(raw, policy=email.policy.SMTP)
        assert isinstance(msg, EmailMessage)
        return drafts, msg

    def _draft_info(self, uid: str) -> Draft:
        conn = self._imap()
        try:
            _, msg = self._load_draft(conn, uid)
            return Draft(
                uid, _addresses(msg.get("To")) + _addresses(msg.get("Cc")), str(msg["Subject"])
            )
        finally:
            with contextlib.suppress(Exception):
                conn.logout()

    def _send(self, uid: str) -> None:
        if not self.s.smtp_host:
            raise ServiceError("no outgoing (SMTP) server is configured for this account")
        conn = self._imap()
        try:
            _, msg = self._load_draft(conn, uid)
            recipients = _addresses(msg.get("To")) + _addresses(msg.get("Cc"))
            if not recipients:
                raise ServiceError("the draft has no recipients")
            ctx = ssl.create_default_context()
            try:
                if self.s.smtp_port == 465:
                    smtp: smtplib.SMTP = smtplib.SMTP_SSL(
                        self.s.smtp_host, self.s.smtp_port, timeout=TIMEOUT_S, context=ctx
                    )
                else:
                    smtp = smtplib.SMTP(self.s.smtp_host, self.s.smtp_port, timeout=TIMEOUT_S)
                    smtp.starttls(context=ctx)
                with smtp:
                    smtp.login(self.s.username, self._password)
                    smtp.send_message(msg, from_addr=self.s.email, to_addrs=recipients)
            except (OSError, smtplib.SMTPException) as exc:
                raise ServiceError(f"sending failed: {exc}") from exc
            # Sent: remove the draft and file a copy in Sent (best effort).
            with contextlib.suppress(Exception):
                conn.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
                conn.expunge()
            with contextlib.suppress(Exception):
                sent = self._special(conn, b"\\Sent", "Sent")
                del msg["Bcc"]
                conn.append(
                    _quote(sent), "(\\Seen)", imaplib.Time2Internaldate(time.time()), msg.as_bytes()
                )
        finally:
            with contextlib.suppress(Exception):
                conn.logout()

    # ---------------------------------------------------------------- async API
    async def account(self) -> str:
        return await asyncio.to_thread(self._verify)

    async def search_mail(self, query: str, limit: int) -> list[MailSummary]:
        return await asyncio.to_thread(self._search, query, limit)

    async def read_mail(self, message_id: str) -> Mail:
        return await asyncio.to_thread(self._read, message_id)

    async def create_draft(
        self, to: list[str], cc: list[str], subject: str, body: str, reply_to: str | None
    ) -> Draft:
        return await asyncio.to_thread(self._draft, to, cc, subject, body, reply_to)

    async def draft_info(self, draft_id: str) -> Draft:
        return await asyncio.to_thread(self._draft_info, draft_id)

    async def send_draft(self, draft_id: str) -> None:
        await asyncio.to_thread(self._send, draft_id)
