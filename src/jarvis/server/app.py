"""Local control API and desktop UI.

Bound to loopback only, guarded against DNS rebinding with a Host allow-list. Every
API route except /health needs the per-install bearer token; the UI's static files
are public (they contain no data) and receive the token via the URL fragment, which
browsers never send to the server.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import time
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from importlib import resources
from typing import Annotated, Any

from fastapi import (
    Body,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from jarvis import __version__
from jarvis.agent.events import Event
from jarvis.core.config import LLMProvider as ProviderName
from jarvis.core.config import Settings
from jarvis.llm.base import LLMError
from jarvis.server.hub import Hub
from jarvis.server.session import AgentFactory, ClientSession, TaskBoard

log = logging.getLogger(__name__)
WS_AUTH_TIMEOUT_S = 5.0

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self' ws://127.0.0.1:* ws://localhost:*; font-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


def ui_dir() -> Any:
    return resources.files("jarvis") / "ui"


def create_app(
    settings: Settings,
    token: str,
    agent_factory: AgentFactory | None = None,
    kill_switch: bool = False,
    services: list[Callable[[TaskBoard], Coroutine[Any, Any, None]]] | None = None,
    hub: Hub | None = None,
) -> FastAPI:
    board = hub.board if hub is not None else TaskBoard()
    factory = hub.agent_factory if hub is not None and agent_factory is None else agent_factory
    clients: set[ClientSession] = set()

    async def broadcast(event: Event) -> None:
        for c in list(clients):
            await c.send(event)

    if hub is not None:
        hub.attach_broadcast(broadcast)

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
        running: list[asyncio.Task[None]] = [asyncio.create_task(s(board)) for s in services or []]
        if hub is not None:
            await hub.startup()
        try:
            yield
        finally:
            if hub is not None:
                await hub.shutdown()
            for t in running:
                t.cancel()
            for t in running:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
            if switch is not None:
                switch.stop()

    app = FastAPI(
        title="JARVIS", version=__version__, docs_url=None, redoc_url=None, lifespan=lifespan
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    app.state.board = board
    app.state.broadcast = broadcast
    started = time.monotonic()

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    def token_ok(candidate: str | None) -> bool:
        return candidate is not None and hmac.compare_digest(candidate, token)

    def require_token(authorization: Annotated[str | None, Header()] = None) -> None:
        scheme, _, value = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token_ok(value):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing token")

    def need_hub() -> Hub:
        if hub is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "not available")
        return hub

    auth = [Depends(require_token)]

    # ------------------------------------------------------------------ basic

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/v1/status", dependencies=auth)
    async def get_status() -> dict[str, Any]:
        out: dict[str, Any] = {
            "version": __version__,
            "uptime_s": round(time.monotonic() - started, 1),
            "llm_provider": settings.llm.provider.value,
            "voice_enabled": settings.voice.enabled,
            "agent_available": factory is not None,
            "busy": board.busy,
            "kill_hotkey": settings.safety.kill_hotkey,
        }
        if hub is not None:
            out["setup"] = hub.setup_status()
        return out

    @app.post("/v1/stop", dependencies=auth)
    async def stop_all() -> dict[str, int]:
        return {"cancelled": board.cancel_all()}

    # ------------------------------------------------------------------ data

    @app.get("/v1/tasks", dependencies=auth)
    async def list_tasks(limit: int = 30) -> list[dict[str, Any]]:
        store = need_hub().store
        if store is None:
            return []
        from jarvis.memory.store import RESUMABLE

        return [
            {**t.__dict__, "resumable": t.status in RESUMABLE}
            for t in store.tasks(max(1, min(limit, 200)))
        ]

    @app.get("/v1/memories", dependencies=auth)
    async def list_memories() -> list[dict[str, Any]]:
        store = need_hub().store
        return [] if store is None else [m.__dict__ for m in store.memories()]

    @app.delete("/v1/memories/{memory_id}", dependencies=auth)
    async def delete_memory(memory_id: int) -> dict[str, bool]:
        store = need_hub().store
        if store is None or not store.forget(memory_id):
            raise HTTPException(404, "no such memory")
        return {"deleted": True}

    @app.get("/v1/workflows", dependencies=auth)
    async def list_workflows() -> list[dict[str, Any]]:
        store = need_hub().store
        return [] if store is None else [w.__dict__ for w in store.workflows()]

    @app.delete("/v1/workflows/{name}", dependencies=auth)
    async def delete_workflow(name: str) -> dict[str, bool]:
        store = need_hub().store
        if store is None or not store.delete_workflow(name):
            raise HTTPException(404, "no such workflow")
        return {"deleted": True}

    # ------------------------------------------------------------------ setup + voice

    @app.post("/v1/setup/api-key", dependencies=auth)
    async def setup_api_key(key: Annotated[str, Body(embed=True)]) -> dict[str, Any]:
        h = need_hub()
        try:
            await h.set_api_key(key)
        except LLMError as exc:
            raise HTTPException(400, str(exc)) from exc
        return h.setup_status()

    @app.post("/v1/setup/provider", dependencies=auth)
    async def setup_provider(
        provider: Annotated[ProviderName, Body()],
        ollama_model: Annotated[str | None, Body()] = None,
    ) -> dict[str, Any]:
        h = need_hub()
        await h.use_provider(provider, ollama_model)
        if provider is ProviderName.OLLAMA:
            ok, detail = await h.provider().check()
            if not ok:
                raise HTTPException(400, f"Ollama: {detail}")
        return h.setup_status()

    @app.post("/v1/setup/voice-models", dependencies=auth, status_code=202)
    async def setup_voice_models() -> dict[str, str]:
        h = need_hub()
        if h.setup_progress is not None:
            return {"status": "already downloading"}
        h.setup_progress = {"file": "", "done": 0, "total": 1}
        task = asyncio.create_task(h.download_voice_models())
        board.track(task)
        return {"status": "started"}

    @app.get("/v1/settings", dependencies=auth)
    async def get_settings() -> dict[str, Any]:
        return need_hub().settings_view()

    @app.put("/v1/settings", dependencies=auth)
    async def put_settings(changes: Annotated[dict[str, dict[str, Any]], Body()]) -> dict[str, Any]:
        try:
            return await need_hub().update_settings(changes)
        except ValueError as exc:  # includes pydantic ValidationError
            raise HTTPException(422, str(exc).splitlines()[0] if str(exc) else "invalid") from exc

    @app.post("/v1/voice/preview", dependencies=auth)
    async def preview_voice(
        voice: Annotated[str, Body()], speed: Annotated[float, Body()] = 1.0
    ) -> dict[str, bool]:
        try:
            await need_hub().preview_voice(voice, speed)
        except ValueError as exc:
            raise HTTPException(422, "unknown voice") from exc
        except Exception as exc:
            log.exception("voice preview failed")
            raise HTTPException(500, f"preview failed: {exc}") from exc
        return {"played": True}

    @app.post("/v1/voice", dependencies=auth)
    async def set_voice(enabled: Annotated[bool, Body(embed=True)]) -> dict[str, Any]:
        h = need_hub()
        await h.set_voice_enabled(enabled)
        return h.setup_status()

    # ------------------------------------------------------------------ websocket

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
        gen = (lambda: hub.generation) if hub is not None else (lambda: 0)
        session = ClientSession(websocket, factory, board, gen)
        clients.add(session)
        await session.send({"type": "ready", "version": __version__, "busy": board.busy})
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
            clients.discard(session)
            await session.close()

    # ------------------------------------------------------------------ desktop UI

    static = ui_dir()
    if static.is_dir():
        app.mount("/ui", StaticFiles(directory=str(static)), name="ui")

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(str(static / "index.html"), media_type="text/html")

    return app
