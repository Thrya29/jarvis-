"""Connected accounts: which ones exist, what each may do, and live access to them.

Non-secret details (provider, address, ticked capabilities) are kept in
``connections.json``; tokens and app passwords are DPAPI-encrypted in ``connections/``.
Each account only gets the permissions for the capabilities the user ticked, so
"read my calendar" never grants access to mail.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
import webbrowser
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import httpx2 as httpx

from jarvis.connect.imap import ImapService, ImapSettings
from jarvis.connect.oauth import OAuthClient, OAuthError, Token, authorize, refresh
from jarvis.connect.secure_store import SecureStore
from jarvis.connect.services import GoogleService, MicrosoftService, ServiceError
from jarvis.core.config import ConnectionsConfig

log = logging.getLogger(__name__)


class Provider(StrEnum):
    MICROSOFT = "microsoft"
    GOOGLE = "google"
    IMAP = "imap"


class Capability(StrEnum):
    MAIL_READ = "mail_read"
    MAIL_DRAFT = "mail_draft"
    MAIL_SEND = "mail_send"
    CALENDAR_READ = "calendar_read"
    CALENDAR_WRITE = "calendar_write"
    FILES = "files"


CAPABILITY_LABELS = {
    Capability.MAIL_READ: "Read email",
    Capability.MAIL_DRAFT: "Write drafts",
    Capability.MAIL_SEND: "Send email (always asks first)",
    Capability.CALENDAR_READ: "Read calendar",
    Capability.CALENDAR_WRITE: "Create events (asks before inviting people)",
    Capability.FILES: "Search and download files (OneDrive / Google Drive)",
}

# Provider permission scopes per capability: the smallest set that works.
SCOPES: dict[Provider, dict[Capability, list[str]]] = {
    Provider.MICROSOFT: {
        Capability.MAIL_READ: ["Mail.Read"],
        Capability.MAIL_DRAFT: ["Mail.ReadWrite"],
        Capability.MAIL_SEND: ["Mail.ReadWrite", "Mail.Send"],
        Capability.CALENDAR_READ: ["Calendars.Read"],
        Capability.CALENDAR_WRITE: ["Calendars.ReadWrite"],
        Capability.FILES: ["Files.Read.All"],
    },
    Provider.GOOGLE: {
        Capability.MAIL_READ: ["https://www.googleapis.com/auth/gmail.readonly"],
        Capability.MAIL_DRAFT: ["https://www.googleapis.com/auth/gmail.compose"],
        Capability.MAIL_SEND: ["https://www.googleapis.com/auth/gmail.compose"],
        Capability.CALENDAR_READ: ["https://www.googleapis.com/auth/calendar.readonly"],
        Capability.CALENDAR_WRITE: ["https://www.googleapis.com/auth/calendar.events"],
        Capability.FILES: ["https://www.googleapis.com/auth/drive.readonly"],
    },
    Provider.IMAP: {
        Capability.MAIL_READ: [],
        Capability.MAIL_DRAFT: [],
        Capability.MAIL_SEND: [],
    },
}
BASE_SCOPES = {
    Provider.MICROSOFT: ["offline_access", "User.Read"],
    Provider.GOOGLE: ["openid", "email"],
    Provider.IMAP: [],
}
GOOGLE_REVOKE = "https://oauth2.googleapis.com/revoke"


class AccountError(RuntimeError):
    pass


def scopes_for(provider: Provider, caps: list[Capability]) -> list[str]:
    out = list(BASE_SCOPES[provider])
    for cap in caps:
        for scope in SCOPES[provider].get(cap, []):
            if scope not in out:
                out.append(scope)
    return out


def oauth_client(provider: Provider, cfg: ConnectionsConfig) -> OAuthClient:
    if provider is Provider.MICROSOFT:
        base = f"https://login.microsoftonline.com/{cfg.microsoft_tenant}/oauth2/v2.0"
        return OAuthClient(
            "Microsoft",
            f"{base}/authorize",
            f"{base}/token",
            cfg.microsoft_client_id.strip(),
            extra_auth_params={"prompt": "select_account"},
        )
    if provider is Provider.GOOGLE:
        return OAuthClient(
            "Google",
            "https://accounts.google.com/o/oauth2/v2/auth",
            "https://oauth2.googleapis.com/token",
            cfg.google_client_id.strip(),
            cfg.google_client_secret.strip() or None,
            # offline + consent: always return a refresh token, and show what's granted.
            extra_auth_params={"access_type": "offline", "prompt": "consent"},
        )
    raise AccountError("IMAP accounts don't use sign-in")


@dataclass
class Account:
    id: str
    provider: Provider
    email: str
    capabilities: list[Capability]
    connected_at: float = field(default_factory=time.time)
    imap: dict[str, Any] | None = None  # server settings for IMAP accounts

    def can(self, cap: Capability) -> bool:
        return cap in self.capabilities

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        d["provider"] = self.provider.value
        d["capabilities"] = [c.value for c in self.capabilities]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Account:
        return cls(
            d["id"],
            Provider(d["provider"]),
            d["email"],
            [
                Capability(c)
                for c in d.get("capabilities", [])
                if c in Capability._value2member_map_
            ],
            float(d.get("connected_at", 0)),
            d.get("imap"),
        )


def account_id(provider: Provider, email: str) -> str:
    return f"{provider.value}_{hashlib.sha256(email.lower().encode()).hexdigest()[:10]}"


Service = MicrosoftService | GoogleService | ImapService


class ConnectionManager:
    def __init__(
        self,
        cfg: Callable[[], ConnectionsConfig],
        data_dir: Path,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._cfg = cfg
        self._file = data_dir / "connections.json"
        self._secrets = SecureStore(data_dir / "connections")
        self._transport = transport  # tests inject a mock transport
        self._signin = asyncio.Lock()
        self._refresh_locks: dict[str, asyncio.Lock] = {}

    # ---------------------------------------------------------------- registry
    def accounts(self) -> list[Account]:
        try:
            raw = json.loads(self._file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            log.warning("connections.json is unreadable; ignoring it")
            return []
        out = []
        for item in raw.get("accounts", []):
            with contextlib.suppress(KeyError, ValueError, TypeError):
                out.append(Account.from_dict(item))
        return out

    def _save_accounts(self, accounts: list[Account]) -> None:
        tmp = self._file.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"accounts": [a.public() for a in accounts]}, indent=2), encoding="utf-8"
        )
        tmp.replace(self._file)

    def _upsert(self, account: Account) -> None:
        accounts = [a for a in self.accounts() if a.id != account.id]
        accounts.append(account)
        self._save_accounts(accounts)

    def status(self) -> dict[str, Any]:
        cfg = self._cfg()
        return {
            "accounts": [a.public() for a in self.accounts()],
            "apps": {
                "microsoft": bool(cfg.microsoft_client_id.strip()),
                "google": bool(cfg.google_client_id.strip()),
            },
            "capabilities": {c.value: label for c, label in CAPABILITY_LABELS.items()},
            "provider_capabilities": {p.value: [c.value for c in SCOPES[p]] for p in Provider},
        }

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self._transport, follow_redirects=False)

    # ---------------------------------------------------------------- connect
    async def connect(
        self,
        provider: Provider,
        capabilities: list[Capability],
        open_browser: Callable[[str], Any] = webbrowser.open,
        wait_s: float = 300.0,
    ) -> Account:
        """Sign in on the provider's page and store the account."""
        caps = self._check_caps(provider, capabilities)
        client = oauth_client(provider, self._cfg())
        if not client.client_id:
            raise AccountError(
                f"{client.name} sign-in isn't set up in this copy of JARVIS yet: add the app "
                "ID under Connections → Advanced (see docs/connections-setup.md)"
            )
        if self._signin.locked():
            raise AccountError("another sign-in is already in progress")
        async with self._signin, self._http() as http:
            try:
                token = await authorize(
                    client, scopes_for(provider, caps), http, open_browser, wait_s
                )
            except OAuthError as exc:
                raise AccountError(str(exc)) from exc
            svc: MicrosoftService | GoogleService = (
                MicrosoftService(http, token.access_token)
                if provider is Provider.MICROSOFT
                else GoogleService(http, token.access_token)
            )
            try:
                email = await svc.account()
            except ServiceError as exc:
                raise AccountError(f"signed in, but couldn't read the account: {exc}") from exc
        if not email:
            raise AccountError("signed in, but the provider didn't return an email address")
        account = Account(account_id(provider, email), provider, email, caps)
        self._secrets.save(account.id, {"token": token.to_dict()})
        self._upsert(account)
        log.info("connected %s account (%s)", provider.value, ",".join(caps))
        return account

    async def connect_imap(
        self, settings: ImapSettings, password: str, capabilities: list[Capability]
    ) -> Account:
        caps = self._check_caps(Provider.IMAP, capabilities)
        if Capability.MAIL_SEND in caps and not settings.smtp_host:
            raise AccountError("sending needs an outgoing (SMTP) server")
        svc = ImapService(settings, password)
        try:
            await svc.account()  # verifies the server and the password
        except ServiceError as exc:
            raise AccountError(str(exc)) from exc
        account = Account(
            account_id(Provider.IMAP, settings.email),
            Provider.IMAP,
            settings.email,
            caps,
            imap=settings.to_dict(),
        )
        self._secrets.save(account.id, {"password": password})
        self._upsert(account)
        return account

    async def disconnect(self, account_id_: str) -> bool:
        accounts = self.accounts()
        match = [a for a in accounts if a.id == account_id_]
        if not match:
            return False
        account = match[0]
        secret = self._secrets.load(account.id) or {}
        if account.provider is Provider.GOOGLE and "token" in secret:
            # Revoke at Google too, so the grant disappears from the user's account page.
            token = Token.from_dict(secret["token"])
            with contextlib.suppress(httpx.HTTPError):
                async with self._http() as http:
                    await http.post(
                        GOOGLE_REVOKE,
                        data={"token": token.refresh_token or token.access_token},
                        timeout=15,
                    )
        self._secrets.delete(account.id)
        self._save_accounts([a for a in accounts if a.id != account.id])
        log.info("disconnected %s account", account.provider.value)
        return True

    def check(self, provider: Provider, capabilities: list[Capability]) -> None:
        """Validate a sign-in request before opening the browser."""
        self._check_caps(provider, capabilities)
        if provider is not Provider.IMAP and not oauth_client(provider, self._cfg()).client_id:
            raise AccountError(
                f"{provider.value.title()} sign-in isn't set up in this copy of JARVIS yet: add "
                "the app ID under Connections → Advanced (see docs/connections-setup.md)"
            )

    @staticmethod
    def _check_caps(provider: Provider, caps: list[Capability]) -> list[Capability]:
        unique = list(dict.fromkeys(caps))
        if not unique:
            raise AccountError("tick at least one thing JARVIS may do with this account")
        unsupported = [c.value for c in unique if c not in SCOPES[provider]]
        if unsupported:
            raise AccountError(f"{provider.value} accounts can't do: {', '.join(unsupported)}")
        return unique

    # ---------------------------------------------------------------- use
    def pick(self, cap: Capability, account: str | None = None) -> Account:
        """The account to use for ``cap``: the one named, else the first that allows it."""
        accounts = self.accounts()
        if not accounts:
            raise AccountError(
                "no accounts are connected. The user can add one in JARVIS → Connections."
            )
        if account:
            wanted = account.strip().lower()
            named = [a for a in accounts if wanted in (a.id.lower(), a.email.lower())]
            if not named:
                known = ", ".join(a.email for a in accounts)
                raise AccountError(f"no connected account {account!r} (connected: {known})")
            if not named[0].can(cap):
                raise AccountError(
                    f"{named[0].email} isn't allowed to {CAPABILITY_LABELS[cap].lower()}. The user "
                    "can tick it in Connections."
                )
            return named[0]
        for a in accounts:
            if a.can(cap):
                return a
        raise AccountError(
            f"no connected account is allowed to {CAPABILITY_LABELS[cap].lower()}. The user can "
            "tick it for an account in Connections."
        )

    async def _token(self, account: Account, http: httpx.AsyncClient) -> str:
        lock = self._refresh_locks.setdefault(account.id, asyncio.Lock())
        async with lock:
            secret = self._secrets.load(account.id)
            if not secret or "token" not in secret:
                raise AccountError(f"{account.email} needs to be connected again")
            token = Token.from_dict(secret["token"])
            if token.expired:
                try:
                    token = await refresh(oauth_client(account.provider, self._cfg()), token, http)
                except OAuthError as exc:
                    raise AccountError(f"{account.email}: {exc}") from exc
                self._secrets.save(account.id, {"token": token.to_dict()})
            return token.access_token

    @contextlib.asynccontextmanager
    async def open(
        self, cap: Capability, account: str | None = None
    ) -> AsyncIterator[tuple[Account, Service]]:
        acct = self.pick(cap, account)
        if acct.provider is Provider.IMAP:
            secret = self._secrets.load(acct.id) or {}
            if not acct.imap or "password" not in secret:
                raise AccountError(f"{acct.email} needs to be connected again")
            yield acct, ImapService(ImapSettings.from_dict(acct.imap), secret["password"])
            return
        async with self._http() as http:
            access = await self._token(acct, http)
            svc: Service = (
                MicrosoftService(http, access)
                if acct.provider is Provider.MICROSOFT
                else GoogleService(http, access)
            )
            yield acct, svc
