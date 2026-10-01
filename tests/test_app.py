from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from jarvis.agent.events import EventSink
from jarvis.agent.factory import all_tools
from jarvis.agent.loop import Agent
from jarvis.app import window
from jarvis.app.icon import icon_image, write_ico
from jarvis.core.config import AgentConfig, LLMProvider, Settings, load_settings
from jarvis.core.paths import AppPaths
from jarvis.llm.base import LLMError
from jarvis.memory.store import MemoryKind
from jarvis.safety.policy import Approver
from jarvis.server.app import create_app
from jarvis.server.hub import Hub, update_config_file
from jarvis.tools.base import ToolRegistry
from tests.fakes import ScriptedProvider, make_ctx, text

TOKEN = "t" * 43
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def hub(paths: AppPaths) -> Hub:
    return Hub(load_settings(), paths)


@pytest.fixture
def client(hub: Hub) -> TestClient:
    return TestClient(create_app(hub.settings, TOKEN, hub=hub), base_url="http://127.0.0.1")


# ======================================================================= config writes


def test_update_config_file_merges(paths: AppPaths) -> None:
    paths.config_file.write_text('[llm]\nprovider = "anthropic"\n[voice]\nbarge_in = false\n')
    update_config_file(paths, {"llm": {"provider": "ollama"}, "llm.ollama": {"model": "x:1b"}})
    data = tomllib.loads(paths.config_file.read_text())
    assert data["llm"]["provider"] == "ollama" and data["llm"]["ollama"]["model"] == "x:1b"
    assert data["voice"]["barge_in"] is False  # untouched
    assert load_settings().llm.provider is LLMProvider.OLLAMA


# ======================================================================= UI + security


def test_ui_is_served_with_strict_csp(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200 and "<title>JARVIS</title>" in r.text
    csp = r.headers["content-security-policy"]
    assert (
        "script-src 'self'" in csp
        and "unsafe-inline" not in csp
        and "frame-ancestors 'none'" in csp
    )
    assert "worker-src 'self'" in csp and "blob:" not in csp.split("worker-src")[1].split(";")[0]
    # The built bundle (React app) is referenced from index.html and served.
    assets = re.findall(r'(?:src|href)="(/ui/assets/[^"]+)"', r.text)
    assert any(a.endswith(".js") for a in assets) and any(a.endswith(".css") for a in assets)
    for asset in assets:
        got = client.get(asset)
        assert got.status_code == 200, asset
    assert "<script>" not in r.text and "style=" not in r.text  # nothing inline (CSP)


FRONTEND = Path(__file__).parents[1] / "frontend" / "src"


def test_ui_never_injects_markup() -> None:
    """Our UI code renders text only; markup injection APIs are banned outright."""
    sources = [p for p in FRONTEND.rglob("*") if p.suffix in {".ts", ".tsx"}]
    assert sources, "frontend sources missing"
    for path in sources:
        code = path.read_text(encoding="utf-8")
        for banned in (
            "dangerouslySetInnerHTML=",
            ".innerHTML",
            "outerHTML",
            "insertAdjacentHTML",
            "eval(",
            "new Function(",
        ):
            assert banned not in code, f"{banned} in {path.name}"


def test_built_ui_is_present() -> None:
    """src/jarvis/ui is the committed build of frontend/ (CI rebuilds it and compares)."""
    ui = Path(__file__).parents[1] / "src" / "jarvis" / "ui"
    assert (ui / "index.html").exists() and (ui / "icon.svg").exists()
    assert any((ui / "assets").glob("maplibre-gl-worker-*.js"))


def test_api_requires_token(client: TestClient) -> None:
    for path in ("/v1/status", "/v1/tasks", "/v1/memories", "/v1/workflows"):
        assert client.get(path).status_code == 401
    assert client.post("/v1/stop").status_code == 401
    assert client.post("/v1/setup/api-key", json={"key": "x"}).status_code == 401


# ======================================================================= status + data


def test_status_reports_setup(client: TestClient) -> None:
    s = client.get("/v1/status", headers=AUTH).json()
    assert s["setup"]["provider"] == "anthropic"
    assert s["setup"]["api_key_set"] is False and s["setup"]["llm_ready"] is False
    assert s["setup"]["voice_state"] == "off"


def test_data_endpoints(client: TestClient, hub: Hub) -> None:
    store = hub.store
    assert store is not None
    m = store.remember(MemoryKind.FACT, "Works at Jane Aerospace")
    store.save_workflow("standup", "Daily notes", "Notes for {day}.", ["day"])
    store.task_started("t1", "Do the thing")
    store.task_finished("t1", "failed", "Oops", 1)
    tasks = client.get("/v1/tasks", headers=AUTH).json()
    assert tasks[0]["id"] == "t1" and tasks[0]["resumable"] is True
    assert (
        client.get("/v1/memories", headers=AUTH).json()[0]["content"] == "Works at Jane Aerospace"
    )
    assert client.get("/v1/workflows", headers=AUTH).json()[0]["parameters"] == ["day"]
    assert client.delete(f"/v1/memories/{m.id}", headers=AUTH).status_code == 200
    assert client.delete(f"/v1/memories/{m.id}", headers=AUTH).status_code == 404
    assert client.delete("/v1/workflows/standup", headers=AUTH).status_code == 200


# ======================================================================= setup flow


def test_api_key_validated_then_stored(
    client: TestClient, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jarvis.llm.anthropic_provider import AnthropicProvider

    r = client.post("/v1/setup/api-key", headers=AUTH, json={"key": "not-a-key"})
    assert r.status_code == 400 and "sk-ant-" in r.json()["detail"]

    async def bad_check(self: Any) -> tuple[bool, str]:
        return False, "API key rejected"

    monkeypatch.setattr(AnthropicProvider, "check", bad_check)
    r = client.post("/v1/setup/api-key", headers=AUTH, json={"key": "sk-ant-" + "x" * 40})
    assert r.status_code == 400 and "rejected" in r.json()["detail"]

    async def good_check(self: Any) -> tuple[bool, str]:
        return True, "ok"

    monkeypatch.setattr(AnthropicProvider, "check", good_check)
    gen = hub.generation
    r = client.post("/v1/setup/api-key", headers=AUTH, json={"key": "sk-ant-" + "y" * 40})
    assert r.status_code == 200 and r.json()["api_key_set"] and r.json()["llm_ready"]
    assert hub.generation == gen + 1  # sessions will rebuild their agents


def test_switch_to_ollama_persists(
    client: TestClient, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jarvis.llm.ollama_provider import OllamaProvider

    async def ok(self: Any) -> tuple[bool, str]:
        return True, "ready"

    monkeypatch.setattr(OllamaProvider, "check", ok)
    r = client.post(
        "/v1/setup/provider",
        headers=AUTH,
        json={"provider": "ollama", "ollama_model": "llama3.2:3b"},
    )
    assert r.status_code == 200 and r.json()["provider"] == "ollama" and r.json()["llm_ready"]
    assert load_settings().llm.ollama.model == "llama3.2:3b"


def test_voice_toggle(client: TestClient, hub: Hub, monkeypatch: pytest.MonkeyPatch) -> None:
    calls_made: list[str] = []

    async def fake_start(self: Hub) -> None:
        calls_made.append("start")

    async def fake_stop(self: Hub) -> None:
        calls_made.append("stop")

    monkeypatch.setattr(Hub, "start_voice", fake_start)
    monkeypatch.setattr(Hub, "stop_voice", fake_stop)
    assert client.post("/v1/voice", headers=AUTH, json={"enabled": True}).json()["voice_enabled"]
    assert load_settings().voice.enabled is True
    client.post("/v1/voice", headers=AUTH, json={"enabled": False})
    assert calls_made == ["start", "stop"] and load_settings().voice.enabled is False


# ======================================================================= websocket


def _auth(ws: Any) -> dict[str, Any]:
    ws.send_json({"type": "auth", "token": TOKEN})
    return dict(ws.receive_json())


def test_setup_required_error_when_no_model(client: TestClient) -> None:
    with client.websocket_connect("ws://127.0.0.1/v1/ws") as ws:
        assert _auth(ws)["type"] == "ready"
        ws.send_json({"type": "task.start", "goal": "hello"})
        err = ws.receive_json()
        assert err["type"] == "error" and err["setup_required"] is True


def test_agent_rebuilt_after_setup(tmp_path: Path, paths: AppPaths) -> None:
    hub = Hub(load_settings(), paths)
    built: list[int] = []

    def factory(approver: Approver, emit: EventSink) -> Agent:
        built.append(hub.generation)
        ctx = make_ctx(tmp_path / "docs", approver)
        return Agent(
            ScriptedProvider([text("one"), text("two")]),
            ToolRegistry(all_tools()),
            ctx,
            AgentConfig(),
            emit,
        )

    hub.agent_factory = factory  # type: ignore[method-assign]
    client = TestClient(create_app(hub.settings, TOKEN, hub=hub), base_url="http://127.0.0.1")
    with client.websocket_connect("ws://127.0.0.1/v1/ws") as ws:
        _auth(ws)
        ws.send_json({"type": "task.start", "goal": "a"})
        while ws.receive_json()["type"] != "task.finished":
            pass
        hub.generation += 1  # e.g. the user switched model in Setup
        ws.send_json({"type": "task.start", "goal": "b"})
        while ws.receive_json()["type"] != "task.finished":
            pass
    assert built == [0, 1]


def test_broadcast_reaches_all_clients(hub: Hub) -> None:
    app = create_app(hub.settings, TOKEN, hub=hub)
    url = "ws://127.0.0.1/v1/ws"
    with (
        TestClient(app, base_url="http://127.0.0.1") as client,
        client.websocket_connect(url) as a,
        client.websocket_connect(url) as b,
    ):
        _auth(a)
        _auth(b)
        event = {"type": "voice.state", "state": "listening"}
        client.portal.call(hub.broadcast, event)  # type: ignore[union-attr]
        assert a.receive_json() == event
        assert b.receive_json() == event


def test_stop_endpoint(client: TestClient) -> None:
    assert client.post("/v1/stop", headers=AUTH).json() == {"cancelled": 0}


# ======================================================================= window + icon


def test_edge_command_uses_private_profile(tmp_path: Path) -> None:
    url = window.ui_url(8765, "abc")
    assert url == "http://127.0.0.1:8765/#token=abc"  # token only in the fragment
    cmd = window.edge_command(Path("msedge.exe"), url, tmp_path / "profile")
    assert f"--app={url}" in cmd and f"--user-data-dir={tmp_path / 'profile'}" in cmd


def test_open_window_falls_back_to_browser(
    paths: AppPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(window, "find_edge", lambda: None)
    monkeypatch.setattr(window.webbrowser, "open", opened.append)
    window.open_window(paths, 8765, "tok")
    assert opened == ["http://127.0.0.1:8765/#token=tok"]


def test_second_launch_just_opens_window(paths: AppPaths, monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvis.core.instance import InstanceLock
    from jarvis.server import daemon

    opened: list[int] = []
    monkeypatch.setattr(daemon, "_healthy", lambda s, timeout=1.0: True)
    monkeypatch.setattr("jarvis.app.window.open_window", lambda p, port, tok: opened.append(port))
    with InstanceLock(paths.lock_file):  # another JARVIS is running
        assert daemon.serve(Settings(), paths, desktop=True) == 0
        # The minimized autostart launch must not pop a window.
        assert daemon.serve(Settings(), paths, desktop=True, show=False) == 0
    assert opened == [8765]


def test_icon(tmp_path: Path) -> None:
    img = icon_image(32)
    assert img.size == (32, 32) and img.mode == "RGBA"
    write_ico(str(tmp_path / "j.ico"))
    assert (tmp_path / "j.ico").stat().st_size > 1000


def test_llm_error_type_for_missing_key(hub: Hub) -> None:
    with pytest.raises(LLMError):
        hub.provider()
