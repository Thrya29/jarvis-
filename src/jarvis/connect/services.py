"""Mail, calendar and file access for connected accounts (Microsoft Graph, Google APIs)."""

from __future__ import annotations

import base64
import email.utils
import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import httpx2 as httpx

GRAPH = "https://graph.microsoft.com/v1.0"
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
GCAL = "https://www.googleapis.com/calendar/v3/calendars/primary"
GDRIVE = "https://www.googleapis.com/drive/v3"
MAX_BODY_CHARS = 20_000
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024


class ServiceError(RuntimeError):
    pass


@dataclass
class MailSummary:
    id: str
    sender: str
    subject: str
    date: str
    snippet: str
    unread: bool = False


@dataclass
class Mail:
    id: str
    sender: str
    to: list[str]
    cc: list[str]
    subject: str
    date: str
    body: str
    attachments: list[str] = field(default_factory=list)


@dataclass
class Event:
    id: str
    subject: str
    start: str
    end: str
    location: str = ""
    organizer: str = ""
    attendees: list[str] = field(default_factory=list)


@dataclass
class FileItem:
    id: str
    name: str
    modified: str
    size: int
    url: str = ""
    kind: str = ""


@dataclass
class Draft:
    id: str
    to: list[str]
    subject: str


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.out: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in {"script", "style", "head"}:
            self._skip += 1
        if tag in {"br", "p", "div", "tr", "li"}:
            self.out.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "head"} and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.out.append(data)


def html_text(html: str) -> str:
    p = _Text()
    p.feed(html)
    return re.sub(r"\n\s*\n+", "\n\n", "".join(p.out)).strip()


def _local(iso: str) -> str:
    """ISO timestamp (UTC or offset) -> local time, readable."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone().strftime("%a %d %b %Y %H:%M")
    except ValueError:
        return iso


def _clip(text: str) -> str:
    return text if len(text) <= MAX_BODY_CHARS else text[:MAX_BODY_CHARS] + "\n[... truncated ...]"


def _seg(value: str) -> str:
    """Encode an ID for use as one URL path segment (IDs come from the model)."""
    return urllib.parse.quote(value, safe="")


def safe_filename(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(name).name).strip(" .")
    return cleaned or "download"


class _Api:
    def __init__(self, http: httpx.AsyncClient, token: str, label: str) -> None:
        self._http = http
        self._headers = {"Authorization": f"Bearer {token}"}
        self._label = label

    async def call(self, method: str, url: str, **kw: Any) -> Any:
        headers = {**self._headers, **kw.pop("headers", {})}
        try:
            resp = await self._http.request(method, url, headers=headers, timeout=60, **kw)
        except httpx.HTTPError as exc:
            raise ServiceError(f"could not reach {self._label}: {exc}") from exc
        if resp.status_code in (401, 403):
            raise ServiceError(
                f"{self._label} refused the request ({resp.status_code}). The permission may not "
                "have been granted - reconnect the account with that option ticked."
            )
        if resp.status_code >= 400:
            raise ServiceError(f"{self._label} error {resp.status_code}: {resp.text[:300]}")
        return resp

    async def json(self, method: str, url: str, **kw: Any) -> Any:
        resp = await self.call(method, url, **kw)
        return resp.json() if resp.content else {}


# ============================================================================= Microsoft


class MicrosoftService:
    label = "Microsoft 365"

    def __init__(self, http: httpx.AsyncClient, token: str) -> None:
        self.api = _Api(http, token, self.label)

    async def account(self) -> str:
        me = await self.api.json("GET", f"{GRAPH}/me?$select=mail,userPrincipalName")
        return str(me.get("mail") or me.get("userPrincipalName") or "")

    # ---------------------------------------------------------------- mail
    async def search_mail(self, query: str, limit: int) -> list[MailSummary]:
        params = {
            "$top": str(limit),
            "$select": "id,subject,from,receivedDateTime,bodyPreview,isRead",
        }
        if query.strip():
            params["$search"] = '"' + query.replace('"', "") + '"'
            data = await self.api.json(
                "GET",
                f"{GRAPH}/me/messages",
                params=params,
                headers={"ConsistencyLevel": "eventual"},
            )
        else:
            params["$orderby"] = "receivedDateTime desc"
            data = await self.api.json(
                "GET", f"{GRAPH}/me/mailFolders/inbox/messages", params=params
            )
        return [
            MailSummary(
                m["id"],
                (m.get("from") or {}).get("emailAddress", {}).get("address", ""),
                m.get("subject") or "(no subject)",
                _local(m.get("receivedDateTime", "")),
                (m.get("bodyPreview") or "")[:200],
                not m.get("isRead", True),
            )
            for m in data.get("value", [])
        ]

    async def read_mail(self, message_id: str) -> Mail:
        m = await self.api.json(
            "GET",
            f"{GRAPH}/me/messages/{_seg(message_id)}?$select=subject,from,toRecipients,ccRecipients,"
            "receivedDateTime,body,hasAttachments&$expand=attachments($select=name)",
            headers={"Prefer": 'outlook.body-content-type="text"'},
        )

        def addrs(key: str) -> list[str]:
            return [r["emailAddress"]["address"] for r in m.get(key) or []]

        return Mail(
            message_id,
            (m.get("from") or {}).get("emailAddress", {}).get("address", ""),
            addrs("toRecipients"),
            addrs("ccRecipients"),
            m.get("subject") or "",
            _local(m.get("receivedDateTime", "")),
            _clip((m.get("body") or {}).get("content", "")),
            [a.get("name", "") for a in m.get("attachments") or []],
        )

    async def create_draft(
        self, to: list[str], cc: list[str], subject: str, body: str, reply_to: str | None
    ) -> Draft:
        if reply_to:
            d = await self.api.json(
                "POST", f"{GRAPH}/me/messages/{_seg(reply_to)}/createReply", json={"comment": body}
            )
            recipients = [r["emailAddress"]["address"] for r in d.get("toRecipients") or []]
            return Draft(d["id"], recipients, d.get("subject", subject))
        payload = {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": a}} for a in to],
            "ccRecipients": [{"emailAddress": {"address": a}} for a in cc],
        }
        d = await self.api.json("POST", f"{GRAPH}/me/messages", json=payload)
        return Draft(d["id"], to, subject)

    async def draft_info(self, draft_id: str) -> Draft:
        d = await self.api.json(
            "GET", f"{GRAPH}/me/messages/{_seg(draft_id)}?$select=subject,toRecipients,isDraft"
        )
        if not d.get("isDraft", False):
            raise ServiceError("that message is not a draft")
        return Draft(
            draft_id,
            [r["emailAddress"]["address"] for r in d.get("toRecipients") or []],
            d.get("subject", ""),
        )

    async def send_draft(self, draft_id: str) -> None:
        await self.api.call("POST", f"{GRAPH}/me/messages/{_seg(draft_id)}/send")

    # ---------------------------------------------------------------- calendar
    async def events(self, start: datetime, end: datetime, limit: int) -> list[Event]:
        url = (
            f"{GRAPH}/me/calendarView?startDateTime={start.astimezone(UTC).isoformat()}"
            f"&endDateTime={end.astimezone(UTC).isoformat()}&$top={limit}"
            "&$orderby=start/dateTime&$select=subject,start,end,location,organizer,attendees"
        )
        data = await self.api.json("GET", url, headers={"Prefer": 'outlook.timezone="UTC"'})
        return [
            Event(
                e["id"],
                e.get("subject") or "(no title)",
                _local(e["start"]["dateTime"] + "+00:00"),
                _local(e["end"]["dateTime"] + "+00:00"),
                (e.get("location") or {}).get("displayName", ""),
                (e.get("organizer") or {}).get("emailAddress", {}).get("address", ""),
                [a["emailAddress"]["address"] for a in e.get("attendees") or []],
            )
            for e in data.get("value", [])
        ]

    async def create_event(
        self,
        subject: str,
        start: datetime,
        end: datetime,
        attendees: list[str],
        location: str,
        body: str,
    ) -> Event:
        payload = {
            "subject": subject,
            "start": {
                "dateTime": start.astimezone(UTC).replace(tzinfo=None).isoformat(),
                "timeZone": "UTC",
            },
            "end": {
                "dateTime": end.astimezone(UTC).replace(tzinfo=None).isoformat(),
                "timeZone": "UTC",
            },
            "location": {"displayName": location},
            "body": {"contentType": "Text", "content": body},
            "attendees": [{"emailAddress": {"address": a}, "type": "required"} for a in attendees],
        }
        e = await self.api.json("POST", f"{GRAPH}/me/events", json=payload)
        return Event(
            e["id"],
            subject,
            _local(start.isoformat()),
            _local(end.isoformat()),
            location,
            "",
            attendees,
        )

    # ---------------------------------------------------------------- files
    async def search_files(self, query: str, limit: int) -> list[FileItem]:
        q = _seg(query.replace("'", "''"))
        data = await self.api.json(
            "GET",
            f"{GRAPH}/me/drive/root/search(q='{q}')",
            params={
                "$top": str(limit),
                "$select": "id,name,webUrl,lastModifiedDateTime,size,folder",
            },
        )
        return [
            FileItem(
                f["id"],
                f["name"],
                _local(f.get("lastModifiedDateTime", "")),
                int(f.get("size") or 0),
                f.get("webUrl", ""),
                "folder" if "folder" in f else "file",
            )
            for f in data.get("value", [])
        ]

    async def download(self, file_id: str) -> tuple[str, bytes]:
        meta = await self.api.json(
            "GET", f"{GRAPH}/me/drive/items/{_seg(file_id)}?$select=name,size,folder"
        )
        if "folder" in meta:
            raise ServiceError("that's a folder; pick a file")
        if int(meta.get("size") or 0) > MAX_DOWNLOAD_BYTES:
            raise ServiceError("file is larger than 100 MB")
        resp = await self.api.call(
            "GET", f"{GRAPH}/me/drive/items/{_seg(file_id)}/content", follow_redirects=True
        )
        return meta["name"], resp.content


# ============================================================================= Google

EXPORTS = {
    "application/vnd.google-apps.document": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
}  # fmt: skip


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _gmail_body(payload: dict[str, Any]) -> str:
    """Prefer text/plain anywhere in the MIME tree, else stripped HTML."""
    plain: list[str] = []
    html: list[str] = []

    def walk(part: dict[str, Any]) -> None:
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if data:
            text = base64.urlsafe_b64decode(data + "===").decode("utf-8", errors="replace")
            (plain if mime == "text/plain" else html if mime == "text/html" else []).append(text)
        for p in part.get("parts") or []:
            walk(p)

    walk(payload)
    return "\n".join(plain) if plain else html_text("\n".join(html))


class GoogleService:
    label = "Google"

    def __init__(self, http: httpx.AsyncClient, token: str) -> None:
        self.api = _Api(http, token, self.label)

    async def account(self) -> str:
        me = await self.api.json("GET", "https://openidconnect.googleapis.com/v1/userinfo")
        return str(me.get("email", ""))

    # ---------------------------------------------------------------- mail
    async def search_mail(self, query: str, limit: int) -> list[MailSummary]:
        data = await self.api.json(
            "GET", f"{GMAIL}/messages", params={"q": query or "in:inbox", "maxResults": limit}
        )
        out = []
        for m in data.get("messages", [])[:limit]:
            meta = await self.api.json(
                "GET", f"{GMAIL}/messages/{_seg(m['id'])}",
                params=[("format", "metadata"), ("metadataHeaders", "From"),
                        ("metadataHeaders", "Subject"), ("metadataHeaders", "Date")],
            )  # fmt: skip
            h = {x["name"].lower(): x["value"] for x in meta.get("payload", {}).get("headers", [])}
            out.append(
                MailSummary(
                    m["id"],
                    h.get("from", ""),
                    h.get("subject", "(no subject)"),
                    h.get("date", ""),
                    meta.get("snippet", "")[:200],
                    "UNREAD" in meta.get("labelIds", []),
                )
            )
        return out

    async def read_mail(self, message_id: str) -> Mail:
        m = await self.api.json(
            "GET", f"{GMAIL}/messages/{_seg(message_id)}", params={"format": "full"}
        )
        payload = m.get("payload", {})
        h = {x["name"].lower(): x["value"] for x in payload.get("headers", [])}

        def split(v: str) -> list[str]:
            return [a for _, a in email.utils.getaddresses([v]) if a] if v else []

        attachments: list[str] = []

        def walk(p: dict[str, Any]) -> None:
            if p.get("filename"):
                attachments.append(p["filename"])
            for c in p.get("parts") or []:
                walk(c)

        walk(payload)
        return Mail(
            message_id,
            h.get("from", ""),
            split(h.get("to", "")),
            split(h.get("cc", "")),
            h.get("subject", ""),
            h.get("date", ""),
            _clip(_gmail_body(payload)),
            attachments,
        )

    async def create_draft(
        self, to: list[str], cc: list[str], subject: str, body: str, reply_to: str | None
    ) -> Draft:
        msg = EmailMessage()
        thread_id = None
        if reply_to:
            orig = await self.api.json(
                "GET", f"{GMAIL}/messages/{_seg(reply_to)}",
                params=[("format", "metadata"), ("metadataHeaders", "Message-ID"),
                        ("metadataHeaders", "Subject"), ("metadataHeaders", "From")],
            )  # fmt: skip
            h = {x["name"].lower(): x["value"] for x in orig.get("payload", {}).get("headers", [])}
            thread_id = orig.get("threadId")
            subject = subject or h.get("subject", "")
            if not subject.lower().startswith("re:"):
                subject = f"Re: {subject}"
            to = to or [a for _, a in email.utils.getaddresses([h.get("from", "")]) if a]
            if h.get("message-id"):
                msg["In-Reply-To"] = h["message-id"]
                msg["References"] = h["message-id"]
        msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        msg["Subject"] = subject
        msg.set_content(body)
        message: dict[str, Any] = {"raw": _b64url(msg.as_bytes())}
        if thread_id:
            message["threadId"] = thread_id
        d = await self.api.json("POST", f"{GMAIL}/drafts", json={"message": message})
        return Draft(d["id"], to, subject)

    async def draft_info(self, draft_id: str) -> Draft:
        d = await self.api.json(
            "GET", f"{GMAIL}/drafts/{_seg(draft_id)}", params={"format": "metadata"}
        )
        h = {
            x["name"].lower(): x["value"]
            for x in d.get("message", {}).get("payload", {}).get("headers", [])
        }
        return Draft(
            draft_id,
            [a for _, a in email.utils.getaddresses([h.get("to", "")]) if a],
            h.get("subject", ""),
        )

    async def send_draft(self, draft_id: str) -> None:
        await self.api.call("POST", f"{GMAIL}/drafts/send", json={"id": draft_id})

    # ---------------------------------------------------------------- calendar
    async def events(self, start: datetime, end: datetime, limit: int) -> list[Event]:
        data = await self.api.json(
            "GET", f"{GCAL}/events",
            params={"timeMin": start.astimezone(UTC).isoformat(),
                    "timeMax": end.astimezone(UTC).isoformat(),
                    "singleEvents": "true", "orderBy": "startTime", "maxResults": limit},
        )  # fmt: skip
        out = []
        for e in data.get("items", []):
            s, t = e.get("start", {}), e.get("end", {})
            out.append(
                Event(
                    e["id"],
                    e.get("summary", "(no title)"),
                    _local(s.get("dateTime", "")) or s.get("date", ""),
                    _local(t.get("dateTime", "")) or t.get("date", ""),
                    e.get("location", ""),
                    (e.get("organizer") or {}).get("email", ""),
                    [a.get("email", "") for a in e.get("attendees") or []],
                )
            )
        return out

    async def create_event(
        self,
        subject: str,
        start: datetime,
        end: datetime,
        attendees: list[str],
        location: str,
        body: str,
    ) -> Event:
        payload = {
            "summary": subject,
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
            "location": location,
            "description": body,
            "attendees": [{"email": a} for a in attendees],
        }
        e = await self.api.json(
            "POST", f"{GCAL}/events", params={"sendUpdates": "all"}, json=payload
        )
        return Event(
            e["id"],
            subject,
            _local(start.isoformat()),
            _local(end.isoformat()),
            location,
            "",
            attendees,
        )

    # ---------------------------------------------------------------- files
    async def search_files(self, query: str, limit: int) -> list[FileItem]:
        q = query.replace("\\", "\\\\").replace("'", "\\'")
        data = await self.api.json(
            "GET", f"{GDRIVE}/files",
            params={"q": f"(name contains '{q}' or fullText contains '{q}') and trashed = false",
                    "pageSize": limit,
                    "fields": "files(id,name,mimeType,modifiedTime,size,webViewLink)"},
        )  # fmt: skip
        return [
            FileItem(
                f["id"],
                f["name"],
                _local(f.get("modifiedTime", "")),
                int(f.get("size") or 0),
                f.get("webViewLink", ""),
                f.get("mimeType", ""),
            )
            for f in data.get("files", [])
        ]

    async def download(self, file_id: str) -> tuple[str, bytes]:
        meta = await self.api.json(
            "GET", f"{GDRIVE}/files/{_seg(file_id)}", params={"fields": "name,mimeType,size"}
        )
        mime = meta.get("mimeType", "")
        if mime == "application/vnd.google-apps.folder":
            raise ServiceError("that's a folder; pick a file")
        if mime in EXPORTS:
            export_mime, ext = EXPORTS[mime]
            resp = await self.api.call(
                "GET", f"{GDRIVE}/files/{_seg(file_id)}/export", params={"mimeType": export_mime}
            )
            return meta["name"] + ext, resp.content
        if int(meta.get("size") or 0) > MAX_DOWNLOAD_BYTES:
            raise ServiceError("file is larger than 100 MB")
        resp = await self.api.call(
            "GET", f"{GDRIVE}/files/{_seg(file_id)}", params={"alt": "media"}
        )
        return meta["name"], resp.content
