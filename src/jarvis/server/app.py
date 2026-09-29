"""Local control API.

Bound to loopback only, guarded against DNS rebinding with a Host allow-list,
and every non-health route requires the per-install bearer token.
"""

from __future__ import annotations

import asyncio
import hmac
import time
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from jarvis import __version__
from jarvis.core.config import Settings

WS_AUTH_TIMEOUT_S = 5.0


def create_app(settings: Settings, token: str) -> FastAPI:
    app = FastAPI(title="JARVIS", version=__version__, docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    started = time.monotonic()

    def token_ok(candidate: str | None) -> bool:
        return candidate is not None and hmac.compare_digest(candidate, token)

    def require_token(authorization: Annotated[str | None, Header()] = None) -> None:
        scheme, _, value = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token_ok(value):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing token")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/v1/status", dependencies=[Depends(require_token)])
    async def get_status() -> dict[str, Any]:
        return {
            "version": __version__,
            "uptime_s": round(time.monotonic() - started, 1),
            "llm_provider": settings.llm.provider.value,
            "voice_enabled": settings.voice.enabled,
        }

    @app.websocket("/v1/ws")
    async def ws(websocket: WebSocket) -> None:
        # Browsers can't set headers on WebSocket, so the first frame must authenticate.
        await websocket.accept()
        try:
            hello = await asyncio.wait_for(websocket.receive_json(), WS_AUTH_TIMEOUT_S)
        except (TimeoutError, ValueError, WebSocketDisconnect):
            await websocket.close(code=4401)
            return
        if (
            not isinstance(hello, dict)
            or hello.get("type") != "auth"
            or not token_ok(hello.get("token"))
        ):
            await websocket.close(code=4401)
            return
        await websocket.send_json({"type": "ready", "version": __version__})
        try:
            while True:
                msg = await websocket.receive_json()
                if isinstance(msg, dict) and msg.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
                else:
                    await websocket.send_json({"type": "error", "error": "unsupported message"})
        except WebSocketDisconnect:
            return

    return app
