from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from jarvis.core.config import Settings
from jarvis.server.app import create_app

TOKEN = "t" * 43


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(Settings(), TOKEN), base_url="http://127.0.0.1")


def test_health_is_public(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_status_requires_token(client: TestClient) -> None:
    assert client.get("/v1/status").status_code == 401
    assert client.get("/v1/status", headers={"Authorization": "Bearer nope"}).status_code == 401
    r = client.get("/v1/status", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    assert r.json()["llm_provider"] == "anthropic"


def test_foreign_host_header_rejected(client: TestClient) -> None:
    # DNS-rebinding defence: a page on evil.example resolving to 127.0.0.1 is refused.
    assert client.get("/health", headers={"Host": "evil.example"}).status_code == 400


def test_ws_requires_auth(client: TestClient) -> None:
    with client.websocket_connect("ws://127.0.0.1/v1/ws") as ws:
        ws.send_json({"type": "auth", "token": "wrong"})
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == 4401


def test_ws_ping(client: TestClient) -> None:
    with client.websocket_connect("ws://127.0.0.1/v1/ws") as ws:
        ws.send_json({"type": "auth", "token": TOKEN})
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}
