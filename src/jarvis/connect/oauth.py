"""OAuth 2.0 for desktop apps: authorization code + PKCE with a loopback redirect.

The user signs in on the provider's own page in their browser; JARVIS never sees the
password. A one-shot HTTP listener on 127.0.0.1 receives the redirect, the ``state``
value is checked (CSRF), and the code is exchanged using the PKCE verifier, so an
intercepted code is useless to anyone else.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import secrets
import time
import urllib.parse
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx2 as httpx

SIGN_IN_TIMEOUT_S = 300.0

DONE_PAGE = """<!doctype html><meta charset="utf-8"><title>JARVIS</title>
<body style="font-family:Segoe UI,sans-serif;background:#0b1017;color:#e6edf3;display:grid;
place-items:center;height:100vh;margin:0"><div style="text-align:center"><h1>{title}</h1>
<p>{body}</p></div></body>"""


class OAuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class OAuthClient:
    name: str
    auth_url: str
    token_url: str
    client_id: str
    client_secret: str | None = None  # Google "desktop app" clients have a non-secret secret
    extra_auth_params: dict[str, str] = field(default_factory=dict)


@dataclass
class Token:
    access_token: str
    refresh_token: str | None
    expires_at: float
    scope: str = ""

    @property
    def expired(self) -> bool:
        return time.time() > self.expires_at - 60

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "scope": self.scope,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Token:
        return cls(
            d["access_token"], d.get("refresh_token"), float(d["expires_at"]), d.get("scope", "")
        )

    @classmethod
    def from_response(cls, data: dict[str, Any], previous: Token | None = None) -> Token:
        return cls(
            access_token=data["access_token"],
            # Providers may omit the refresh token on refresh; keep the old one then.
            refresh_token=data.get("refresh_token")
            or (previous.refresh_token if previous else None),
            expires_at=time.time() + float(data.get("expires_in", 3600)),
            scope=data.get("scope", previous.scope if previous else ""),
        )


def pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    return verifier, challenge


def _friendly_error(params: dict[str, str]) -> str:
    code = params.get("error", "")
    desc = params.get("error_description", "")
    if "AADSTS65001" in desc or "AADSTS90094" in desc or "admin" in desc.lower():
        return (
            "your organisation requires an administrator to approve JARVIS before you can "
            "connect this account. Ask your IT team, or use a personal account."
        )
    if code == "access_denied":
        return "sign-in was cancelled or permission was declined"
    return f"sign-in failed: {desc or code or 'unknown error'}"


async def _await_redirect(
    state: str,
) -> tuple[int, asyncio.Future[dict[str, str]], asyncio.AbstractServer]:
    loop = asyncio.get_running_loop()
    result: asyncio.Future[dict[str, str]] = loop.create_future()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = (await asyncio.wait_for(reader.readline(), 10)).decode("latin-1")
            while (await reader.readline()) not in (b"\r\n", b"\n", b""):
                pass  # skip headers
            target = line.split(" ")[1] if " " in line else "/"
            params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(target).query))
            if not params:  # e.g. a favicon request
                writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
                return
            ok = params.get("state") == state and "code" in params
            title = "Connected" if ok else "Not connected"
            body = (
                "You can close this tab and return to JARVIS."
                if ok
                else "Something went wrong. Return to JARVIS for details."
            )
            page = DONE_PAGE.format(title=title, body=body).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                + f"Content-Length: {len(page)}\r\nConnection: close\r\n\r\n".encode()
                + page
            )
            if not result.done():
                result.set_result(params)
        except (TimeoutError, ConnectionError, IndexError):
            pass
        finally:
            with contextlib.suppress(ConnectionError):
                await writer.drain()
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return port, result, server


async def authorize(
    client: OAuthClient,
    scopes: list[str],
    http: httpx.AsyncClient,
    open_browser: Callable[[str], Any] = webbrowser.open,
    wait_s: float = SIGN_IN_TIMEOUT_S,
) -> Token:
    """Run the interactive sign-in and return tokens."""
    if not client.client_id:
        raise OAuthError(f"no {client.name} app ID configured")
    state = secrets.token_urlsafe(24)
    verifier, challenge = pkce_pair()
    port, result, server = await _await_redirect(state)
    redirect_uri = f"http://localhost:{port}"
    query = {
        "client_id": client.client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        **client.extra_auth_params,
    }
    try:
        open_browser(f"{client.auth_url}?{urllib.parse.urlencode(query)}")
        try:
            params = await asyncio.wait_for(result, wait_s)
        except TimeoutError as exc:
            raise OAuthError("sign-in timed out (no response from the browser)") from exc
    finally:
        server.close()
    if "error" in params:
        raise OAuthError(_friendly_error(params))
    if params.get("state") != state:
        raise OAuthError("sign-in response didn't match this request (state mismatch)")
    form = {
        "client_id": client.client_id,
        "grant_type": "authorization_code",
        "code": params["code"],
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }
    if client.client_secret:
        form["client_secret"] = client.client_secret
    return Token.from_response(await _token_request(http, client, form))


async def refresh(client: OAuthClient, token: Token, http: httpx.AsyncClient) -> Token:
    if not token.refresh_token:
        raise OAuthError("this connection has expired; connect the account again")
    form = {
        "client_id": client.client_id,
        "grant_type": "refresh_token",
        "refresh_token": token.refresh_token,
    }
    if client.client_secret:
        form["client_secret"] = client.client_secret
    return Token.from_response(await _token_request(http, client, form), previous=token)


async def _token_request(
    http: httpx.AsyncClient, client: OAuthClient, form: dict[str, str]
) -> dict[str, Any]:
    try:
        resp = await http.post(client.token_url, data=form, timeout=30)
    except httpx.HTTPError as exc:
        raise OAuthError(f"could not reach {client.name}: {exc}") from exc
    data: dict[str, Any] = resp.json() if resp.content else {}
    if resp.status_code >= 400 or "access_token" not in data:
        err = data.get("error_description") or data.get("error") or resp.text[:200]
        if data.get("error") == "invalid_grant":
            raise OAuthError("this connection was revoked or expired; connect the account again")
        raise OAuthError(f"{client.name} token request failed: {err}")
    return data
