"""Local control API.

Bound to loopback only, guarded against DNS rebinding with a Host allow-list,
and every non-health route requires the per-install bearer token.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from jarvis import __version__
from jarvis.core.config import Settings
from jarvis.server.session import AgentFactory, ClientSession, TaskBoard

log = logging.getLogger(__name__)
WS_AUTH_TIMEOUT_S = 5.0


def create_app(
    settings: Settings,
    token: str,
    agent_factory: AgentFactory | None = None,
    kill_switch: bool = False,
) -> FastAPI:
    board = TaskBoard()  # one task drives the desktop at a time

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        switch = None
        if kill_switch:
            from jarvis.desktop.killswitch import KillSwitch

            loop = asyncio.get_running_loop()

            def stop_everything() -> None:
                n = board.cancel_all()
                log.warning("kill switch: cancelled %d task(s)", n)

            switch = KillSwitch(
                settings.safety.kill_hotkey,
                lambda: loop.call_soon_threadsafe(stop_everything),
            )
            if switch.start():
                log.info("kill switch armed: %s", settings.safety.kill_hotkey)
        try:
            yield
        finally:
            if switch is not None:
                switch.stop()

    app = FastAPI(
        title="JARVIS", version=__version__, docs_url=None, redoc_url=None, lifespan=lifespan
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    app.state.board = board
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
            "agent_available": agent_factory is not None,
            "busy": board.busy,
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
        session = ClientSession(websocket, agent_factory, board)
        await session.send({"type": "ready", "version": __version__})
        try:
            while True:
                try:
                    msg = await websocket.receive_json()
                except ValueError:
                    await session.send({"type": "error", "error": "invalid JSON"})
                    continue
                if isinstance(msg, dict):
                    await session.handle(msg)
                else:
                    await session.send({"type": "error", "error": "expected a JSON object"})
        except WebSocketDisconnect:
            pass
        finally:
            await session.close()

    return app
