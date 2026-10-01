"""V2.1: connected accounts (OAuth, Microsoft Graph, Google, IMAP), their tools and API."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient

from jarvis.agent.factory import all_tools
from jarvis.agent.prompts import build_system_prompt
from jarvis.connect.accounts import (
    Account,
    AccountError,
    Capability,
    ConnectionManager,
    Provider,
    account_id,
    scopes_for,
)
from jarvis.connect.imap import ImapService, ImapSettings, _body
from jarvis.connect.oauth import OAuthClient, OAuthError, Token, authorize, pkce_pair, refresh
from jarvis.connect.secure_store import SecureStore
from jarvis.connect.services import GoogleService, MicrosoftService, html_text, safe_filename
from jarvis.core.config import ConnectionsConfig, load_settings
from jarvis.core.paths import AppPaths
from jarvis.llm.base import ToolCall
from jarvis.safety.policy import Risk
from jarvis.server.app import create_app
from jarvis.server.hub import Hub
from jarvis.tools.base import ToolRegistry
from jarvis.tools.connected import CONNECTED_TOOLS, CalendarCreate
from tests.fakes import ScriptedApprover, make_ctx

TOKEN = "t" * 43
AUTH = {"Authorization": f"Bearer {TOKEN}"}
Handler = Callable[[httpx.Request], httpx.Response]


def browser_that_signs_in(code: str = "the-code", **override: str) -> Callable[[str], None]:
    """Stands in for the user's browser: follows the provider redirect back to JARVIS."""

    def open_browser(url: str) -> None:
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        open_browser.seen = q  # type: ignore[attr-defined]
        params = {"code": code, "state": q["state"], **override}
        target = f"{q['redirect_uri']}/?{urllib.parse.urlencode(params)}"

        async def hit() -> None:
            async with httpx.AsyncClient() as real:
                await real.get(target)

        open_browser.task = asyncio.ensure_future(hit())  # type: ignore[attr-defined]

    return open_browser


CLIENT = OAuthClient("Test", "https://login.example/auth", "https://login.example/token", "cid")


# ======================================================================= OAuth


async def test_authorize_pkce_roundtrip_over_real_loopback() -> None:
    seen: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        form = dict(urllib.parse.parse_qsl(req.content.decode()))
        seen.update(form)
        return httpx.Response(
            200, json={"access_token": "AT", "refresh_token": "RT", "expires_in": 3600}
        )

    browser = browser_that_signs_in()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        token = await authorize(CLIENT, ["Mail.Read"], http, browser, wait_s=10)
    q = browser.seen  # type: ignore[attr-defined]
    assert q["code_challenge_method"] == "S256" and q["scope"] == "Mail.Read"
    assert q["redirect_uri"].startswith("http://localhost:")
    # The verifier sent with the code matches the challenge sent to the browser.
    digest = hashlib.sha256(seen["code_verifier"].encode()).digest()
    assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == q["code_challenge"]
    assert seen["code"] == "the-code" and seen["grant_type"] == "authorization_code"
    assert token.access_token == "AT" and token.refresh_token == "RT" and not token.expired


async def test_authorize_rejects_wrong_state_and_reports_declines() -> None:
    def never(req: httpx.Request) -> httpx.Response:
        raise AssertionError("must not exchange a code")

    async with httpx.AsyncClient(transport=httpx.MockTransport(never)) as http:
        with pytest.raises(OAuthError, match="state"):
            await authorize(CLIENT, [], http, browser_that_signs_in(state="forged"), wait_s=10)
        with pytest.raises(OAuthError, match="cancelled or permission was declined"):
            await authorize(
                CLIENT, [], http, browser_that_signs_in(error="access_denied"), wait_s=10
            )
        with pytest.raises(OAuthError, match="administrator"):
            await authorize(
                CLIENT, [], http,
                browser_that_signs_in(error="consent_required", error_description="AADSTS65001"),
                wait_s=10,
            )  # fmt: skip
        with pytest.raises(OAuthError, match="timed out"):
            await authorize(CLIENT, [], http, lambda url: None, wait_s=0.2)


async def test_refresh_keeps_refresh_token_and_maps_invalid_grant() -> None:
    old = Token("old", "RT", time.time() - 10, "s")
    assert old.expired

    def ok(req: httpx.Request) -> httpx.Response:
        assert b"refresh_token=RT" in req.content
        return httpx.Response(200, json={"access_token": "new", "expires_in": 3600})

    async with httpx.AsyncClient(transport=httpx.MockTransport(ok)) as http:
        new = await refresh(CLIENT, old, http)
    assert new.access_token == "new" and new.refresh_token == "RT" and new.scope == "s"

    def revoked(req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(revoked)) as http:
        with pytest.raises(OAuthError, match="connect the account again"):
            await refresh(CLIENT, old, http)


def test_pkce_pair_is_random_and_valid() -> None:
    v1, c1 = pkce_pair()
    v2, _ = pkce_pair()
    assert v1 != v2 and 43 <= len(v1) <= 128 and "=" not in c1


def test_secure_store_encrypts(tmp_path: Path) -> None:
    store = SecureStore(tmp_path)
    store.save("acct_1", {"token": "very-secret"})
    assert b"very-secret" not in (tmp_path / "acct_1.bin").read_bytes()
    assert store.load("acct_1") == {"token": "very-secret"}
    with pytest.raises(ValueError):
        store.save("../evil", {})
    assert store.delete("acct_1") and store.load("acct_1") is None


# ======================================================================= manager


def test_scopes_are_minimal_per_capability() -> None:
    assert scopes_for(Provider.MICROSOFT, [Capability.CALENDAR_READ]) == [
        "offline_access", "User.Read", "Calendars.Read",
    ]  # fmt: skip
    google = scopes_for(Provider.GOOGLE, [Capability.MAIL_DRAFT, Capability.MAIL_SEND])
    assert google.count("https://www.googleapis.com/auth/gmail.compose") == 1
    assert not any("readonly" in s for s in google)


def ms_cloud(state: dict[str, Any]) -> Handler:
    """A tiny fake of Microsoft's token endpoint and Graph."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("/token"):
            form = dict(urllib.parse.parse_qsl(req.content.decode()))
            state.setdefault("grants", []).append(form["grant_type"])
            n = len(state["grants"])
            return httpx.Response(
                200,
                json={"access_token": f"AT{n}", "refresh_token": f"RT{n}", "expires_in": 3600},
            )
        state.setdefault("auth", []).append(req.headers.get("authorization"))
        if req.url.path == "/v1.0/me":
            return httpx.Response(200, json={"mail": "Anna@Contoso.com"})
        if req.url.path.endswith("/messages"):
            state["params"] = dict(req.url.params)
            return httpx.Response(200, json={"value": []})
        return httpx.Response(404, text="unexpected " + url)

    return handler


def manager(tmp_path: Path, handler: Handler, cfg: ConnectionsConfig | None = None) -> Any:
    cfg = cfg or ConnectionsConfig(microsoft_client_id="ms-app")
    return ConnectionManager(lambda: cfg, tmp_path, transport=httpx.MockTransport(handler))


async def test_connect_use_refresh_disconnect(tmp_path: Path) -> None:
    state: dict[str, Any] = {}
    mgr = manager(tmp_path, ms_cloud(state))
    browser = browser_that_signs_in()
    account = await mgr.connect(
        Provider.MICROSOFT, [Capability.MAIL_READ, Capability.MAIL_READ], browser, wait_s=10
    )
    assert account.email == "Anna@Contoso.com" and account.capabilities == [Capability.MAIL_READ]
    assert "Mail.Read" in browser.seen["scope"] and "Mail.Send" not in browser.seen["scope"]  # type: ignore[attr-defined]
    # Only non-secret metadata in the JSON; the token is encrypted elsewhere.
    raw = (tmp_path / "connections.json").read_text(encoding="utf-8")
    assert "AT1" not in raw and "RT1" not in raw and "Anna@Contoso.com" in raw

    async with mgr.open(Capability.MAIL_READ, "anna@contoso.com") as (_, svc):
        await svc.search_mail("budget", 5)
    assert state["auth"][-1] == "Bearer AT1"
    assert state["params"]["$search"] == '"budget"'

    # Expired access token -> refreshed once and saved.
    secret = mgr._secrets.load(account.id)
    secret["token"]["expires_at"] = time.time() - 5
    mgr._secrets.save(account.id, secret)
    async with mgr.open(Capability.MAIL_READ) as (_, svc):
        await svc.search_mail("", 5)
    assert state["grants"] == ["authorization_code", "refresh_token"]
    assert state["auth"][-1] == "Bearer AT2"

    with pytest.raises(AccountError, match="isn't allowed"):
        mgr.pick(Capability.MAIL_SEND, "anna@contoso.com")
    with pytest.raises(AccountError, match="no connected account 'bob"):
        mgr.pick(Capability.MAIL_READ, "bob@x.com")

    assert await mgr.disconnect(account.id)
    assert mgr.accounts() == [] and mgr._secrets.load(account.id) is None
    with pytest.raises(AccountError, match="no accounts are connected"):
        mgr.pick(Capability.MAIL_READ)


async def test_connect_requires_app_id_and_capabilities(tmp_path: Path) -> None:
    mgr = manager(tmp_path, ms_cloud({}), ConnectionsConfig())
    with pytest.raises(AccountError, match="app ID"):
        await mgr.connect(Provider.MICROSOFT, [Capability.MAIL_READ], lambda u: None)
    with pytest.raises(AccountError, match="tick at least one"):
        mgr.check(Provider.GOOGLE, [])
    with pytest.raises(AccountError, match="can't do: files"):
        mgr.check(Provider.IMAP, [Capability.FILES])


# ======================================================================= services


def recorder(responses: dict[tuple[str, str], Any]) -> tuple[Handler, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        body = responses.get((req.method, req.url.path))
        if body is None:
            return httpx.Response(404, text=f"no fake for {req.method} {req.url.path}")
        if isinstance(body, bytes):
            return httpx.Response(200, content=body)
        return httpx.Response(200, json=body)

    return handler, seen


async def test_graph_ids_are_path_encoded_and_send_uses_draft() -> None:
    handler, seen = recorder(
        {
            ("POST", "/v1.0/me/messages"): {"id": "D1"},
            ("POST", "/v1.0/me/messages/D1/send"): {},
        }
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        svc = MicrosoftService(http, "tok")
        draft = await svc.create_draft(["a@x.com"], [], "Hi", "Body", None)
        await svc.send_draft(draft.id)
        with pytest.raises(Exception, match="404"):
            await svc.read_mail("../../users/ceo/messages")
    payload = json.loads(seen[0].content)
    assert payload["toRecipients"] == [{"emailAddress": {"address": "a@x.com"}}]
    assert seen[2].url.raw_path.startswith(b"/v1.0/me/messages/..%2F..%2Fusers")


async def test_graph_permission_errors_are_explained() -> None:
    def forbidden(req: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"code": "ErrorAccessDenied"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as http:
        with pytest.raises(Exception, match="reconnect the account"):
            await MicrosoftService(http, "tok").search_files("plan", 5)


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


async def test_gmail_read_prefers_plain_text_and_lists_attachments() -> None:
    message = {
        "payload": {
            "headers": [
                {"name": "From", "value": "Anna <anna@x.com>"},
                {"name": "To", "value": "me@x.com, Bob <bob@x.com>"},
                {"name": "Subject", "value": "Q3"},
            ],
            "mimeType": "multipart/mixed",
            "parts": [
                {"mimeType": "text/html", "body": {"data": _b64("<p>html</p>")}},
                {"mimeType": "text/plain", "body": {"data": _b64("plain body")}},
                {"mimeType": "application/pdf", "filename": "q3.pdf", "body": {}},
            ],
        }
    }
    handler, _ = recorder({("GET", "/gmail/v1/users/me/messages/M1"): message})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        mail = await GoogleService(http, "tok").read_mail("M1")
    assert mail.body == "plain body" and mail.attachments == ["q3.pdf"]
    assert mail.to == ["me@x.com", "bob@x.com"]


async def test_gmail_reply_draft_threads_and_sends() -> None:
    orig = {
        "threadId": "T1",
        "payload": {
            "headers": [
                {"name": "From", "value": "anna@x.com"},
                {"name": "Subject", "value": "Plan"},
                {"name": "Message-ID", "value": "<m1@x>"},
            ]
        },
    }
    handler, seen = recorder(
        {
            ("GET", "/gmail/v1/users/me/messages/M1"): orig,
            ("POST", "/gmail/v1/users/me/drafts"): {"id": "D9"},
            ("POST", "/gmail/v1/users/me/drafts/send"): {},
        }
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        svc = GoogleService(http, "tok")
        draft = await svc.create_draft([], [], "", "Sounds good", "M1")
        await svc.send_draft(draft.id)
    body = json.loads(seen[1].content)["message"]
    raw = base64.urlsafe_b64decode(body["raw"] + "==").decode()
    assert body["threadId"] == "T1" and "In-Reply-To: <m1@x>" in raw
    assert "Subject: Re: Plan" in raw and "To: anna@x.com" in raw
    assert draft.to == ["anna@x.com"] and json.loads(seen[2].content) == {"id": "D9"}


async def test_drive_exports_google_docs_as_office_files() -> None:
    handler, seen = recorder(
        {
            ("GET", "/drive/v3/files/F1"): {
                "name": "Budget",
                "mimeType": "application/vnd.google-apps.spreadsheet",
            },
            ("GET", "/drive/v3/files/F1/export"): b"xlsx-bytes",
        }
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        name, data = await GoogleService(http, "tok").download("F1")
    assert name == "Budget.xlsx" and data == b"xlsx-bytes"
    assert "spreadsheetml" in seen[1].url.params["mimeType"]


def test_text_helpers() -> None:
    assert html_text("<style>x{}</style><p>Hello</p><p>there</p>") == "Hello\nthere"
    assert safe_filename("..\\..\\evil:name?.txt") == "evil_name_.txt"
    assert safe_filename("...") == "download"


# ======================================================================= IMAP


class FakeImap:
    """Just enough IMAP4_SSL for the service's calls."""

    mailbox: ClassVar[dict[str, bytes]] = {}
    appended: ClassVar[list[tuple[str, bytes]]] = []

    def __init__(self, host: str, port: int, ssl_context: Any = None, timeout: Any = None):
        self.host = host

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        import imaplib

        if password != "app-pass":
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")
        return "OK", [b""]

    def logout(self) -> None: ...
    def close(self) -> None: ...

    def select(self, name: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        return "OK", [b"1"]

    def list(self) -> tuple[str, list[bytes]]:
        return "OK", [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasNoChildren \\Drafts) "/" "Entw&APw-rfe"',
        ]

    def uid(self, cmd: str, *args: Any) -> tuple[str, list[Any]]:
        if cmd == "SEARCH":
            return "OK", [b" ".join(k.encode() for k in self.mailbox)]
        if cmd == "FETCH":
            return "OK", [(b"1 (UID " + args[0].encode() + b")", self.mailbox[args[0]]), b")"]
        return "OK", [b""]

    def append(self, mailbox: str, flags: str, date: str, msg: bytes) -> tuple[str, list[bytes]]:
        self.appended.append((mailbox, msg))
        return "OK", [b"[APPENDUID 1 77] done"]


async def test_imap_search_read_and_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    import imaplib

    FakeImap.mailbox = {
        "5": b"From: Anna <anna@x.com>\r\nTo: me@x.com\r\nSubject: Invoice\r\n"
        b"Message-ID: <i5@x>\r\nContent-Type: text/html\r\n\r\n<p>Pay <b>now</b></p>",
    }
    FakeImap.appended = []
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeImap)
    settings = ImapSettings("me@x.com", "me@x.com", "imap.x.com", smtp_host="smtp.x.com")
    svc = ImapService(settings, "app-pass")
    assert await svc.account() == "me@x.com"
    found = await svc.search_mail("invoice", 10)
    assert [m.subject for m in found] == ["Invoice"] and found[0].id == "5"
    mail = await svc.read_mail("5")
    assert mail.body == "Pay now" and mail.to == ["me@x.com"]
    draft = await svc.create_draft([], [], "", "Paid.", "5")
    assert draft.id == "77" and draft.to == ["anna@x.com"] and draft.subject == "Re: Invoice"
    folder, raw = FakeImap.appended[0]
    assert folder == '"Entw&APw-rfe"' and b"In-Reply-To: <i5@x>" in raw
    with pytest.raises(Exception, match="invalid message id"):
        await svc.read_mail("1:*")
    with pytest.raises(Exception, match="app password"):
        await ImapService(settings, "wrong").account()


def test_imap_body_falls_back_to_html() -> None:
    import email
    import email.policy

    msg = email.message_from_bytes(
        b"Content-Type: text/plain; charset=utf-8\r\n\r\nhello", policy=email.policy.default
    )
    assert _body(msg) == "hello"


# ======================================================================= tools


class FakeService:
    def __init__(self) -> None:
        from jarvis.connect.services import Draft, Event

        self.Draft, self.Event = Draft, Event
        self.sent: list[str] = []
        self.files = {"F1": ("report.docx", b"DOCX")}

    async def draft_info(self, draft_id: str) -> Any:
        return self.Draft(draft_id, ["boss@corp.com"], "Real subject")

    async def send_draft(self, draft_id: str) -> None:
        self.sent.append(draft_id)

    async def create_event(self, subject: str, start: Any, end: Any, att: Any, *a: Any) -> Any:
        return self.Event("E1", subject, str(start), str(end))

    async def download(self, file_id: str) -> tuple[str, bytes]:
        return self.files[file_id]


class FakeManager:
    def __init__(self) -> None:
        self.svc = FakeService()
        self.account = Account("microsoft_1", Provider.MICROSOFT, "me@corp.com", list(Capability))

    def accounts(self) -> list[Account]:
        return [self.account]

    def open(self, cap: Capability, account: str | None = None) -> Any:
        import contextlib

        @contextlib.asynccontextmanager
        async def cm() -> Any:
            yield self.account, self.svc

        return cm()


async def test_mail_send_approval_shows_server_recipients(tmp_path: Path) -> None:
    approver = ScriptedApprover(approve=False)
    ctx = make_ctx(tmp_path / "root", approver)
    ctx.connections = FakeManager()
    reg = ToolRegistry(CONNECTED_TOOLS)
    out = await reg.execute(ToolCall("1", "mail_send", {"draft_id": "D1"}), ctx)
    assert out.is_error and "declined" in out.content
    req = approver.requests[0]
    assert req.risk is Risk.EXTERNAL and "boss@corp.com" in req.summary
    assert "From: me@corp.com" in req.details and ctx.connections.svc.sent == []

    approver.approve = True
    out = await reg.execute(ToolCall("2", "mail_send", {"draft_id": "D1"}), ctx)
    assert not out.is_error and ctx.connections.svc.sent == ["D1"]


async def test_calendar_invites_need_approval(tmp_path: Path) -> None:
    tool = CalendarCreate()
    ctx = make_ctx(tmp_path / "root")
    args = tool.args_model.model_validate({"subject": "Sync", "start": "2026-10-02T10:00"})
    assert tool.risk_for(args, ctx) is Risk.WRITE  # type: ignore[arg-type]
    args = tool.args_model.model_validate(
        {"subject": "Sync", "start": "2026-10-02T10:00", "attendees": ["a@x.com"]}
    )
    assert tool.risk_for(args, ctx) is Risk.EXTERNAL  # type: ignore[arg-type]


async def test_file_download_stays_in_allowed_folders(tmp_path: Path) -> None:
    root = tmp_path / "root"
    ctx = make_ctx(root)
    ctx.connections = FakeManager()
    (root / "report.docx").write_bytes(b"old")
    reg = ToolRegistry(CONNECTED_TOOLS)
    out = await reg.execute(
        ToolCall("1", "file_download", {"file_id": "F1", "folder": str(root)}), ctx
    )
    assert not out.is_error, out.content
    assert (root / "report.docx").read_bytes() == b"old"  # never overwritten
    assert (root / "report (2).docx").read_bytes() == b"DOCX"
    out = await reg.execute(
        ToolCall("2", "file_download", {"file_id": "F1", "folder": str(tmp_path)}), ctx
    )
    assert out.is_error


async def test_tools_without_accounts_explain_what_to_do(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path / "root")
    ctx.connections = ConnectionManager(ConnectionsConfig, tmp_path / "data")
    out = await ToolRegistry(CONNECTED_TOOLS).execute(
        ToolCall("1", "mail_search", {"query": "x"}), ctx
    )
    assert out.is_error and "Connections" in out.content


def test_tools_and_prompt_follow_connected_accounts() -> None:
    names = [t.name for t in all_tools(connected=True)]
    assert {"mail_search", "mail_send", "calendar_events", "files_search"} <= set(names)
    assert "mail_send" not in [t.name for t in all_tools()]
    roots = [Path.home()]
    assert "You cannot send email" in build_system_prompt(roots)
    with_send = build_system_prompt(roots, accounts=["me@x.com (google): send email"])
    assert "Connected accounts" in with_send and "You cannot send email" not in with_send
    read_only = build_system_prompt(roots, accounts=["me@x.com (google): read email"])
    assert "You cannot send email" in read_only


# ======================================================================= API


@pytest.fixture
def hub(paths: AppPaths) -> Hub:
    return Hub(load_settings(), paths)


@pytest.fixture
def client(hub: Hub) -> TestClient:
    return TestClient(create_app(hub.settings, TOKEN, hub=hub), base_url="http://127.0.0.1")


def test_connections_api(client: TestClient, hub: Hub, paths: AppPaths) -> None:
    assert client.get("/v1/connections").status_code == 401
    status = client.get("/v1/connections", headers=AUTH).json()
    assert status["accounts"] == [] and status["apps"] == {"microsoft": False, "google": False}
    assert "files" not in status["provider_capabilities"]["imap"]

    r = client.post(
        "/v1/connections/microsoft/connect", json={"capabilities": ["mail_read"]}, headers=AUTH
    )
    assert r.status_code == 400 and "app ID" in r.json()["detail"]
    r = client.post(
        "/v1/connections/google/connect", json={"capabilities": ["teleport"]}, headers=AUTH
    )
    assert r.status_code == 422

    # App IDs are ordinary settings.
    r = client.put(
        "/v1/settings", json={"connections": {"microsoft_client_id": "abc"}}, headers=AUTH
    )
    assert r.status_code == 200 and hub.settings.connections.microsoft_client_id == "abc"
    assert client.get("/v1/connections", headers=AUTH).json()["apps"]["microsoft"] is True

    # Disconnect removes the account and its secret.
    mgr = hub.connections
    acct = Account(account_id(Provider.GOOGLE, "a@x.com"), Provider.GOOGLE, "a@x.com", [])
    mgr._upsert(acct)
    mgr._secrets.save(acct.id, {"token": Token("a", None, 0).to_dict()})
    gen = hub.generation
    assert client.delete(f"/v1/connections/{acct.id}", headers=AUTH).status_code == 200
    assert mgr.accounts() == [] and hub.generation == gen + 1
    assert client.delete(f"/v1/connections/{acct.id}", headers=AUTH).status_code == 404

    r = client.post(
        "/v1/connections/imap",
        json={"email": "not-an-email", "password": "x", "imap_host": "h", "capabilities": []},
        headers=AUTH,
    )
    assert r.status_code == 422


def test_documents_api(client: TestClient) -> None:
    assert client.get("/v1/documents", headers=AUTH).json()["enabled"] is False
    assert client.post("/v1/documents/sync", headers=AUTH).status_code == 400
