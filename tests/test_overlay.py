"""V2.3: the floating overlay (observer clients, cross-window approvals, process control)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from jarvis.app import overlay as overlay_mod
from jarvis.app.overlay import Overlay, find_overlay, overlay_url
from jarvis.core.config import load_settings
from jarvis.core.paths import AppPaths
from jarvis.server.app import create_app
from jarvis.server.hub import Hub
from tests.fakes import calls, text
from tests.test_server_tasks import TOKEN, _client, _collect_until

URL = "ws://127.0.0.1/v1/ws"


def _auth(ws: Any, observe: bool = False) -> None:
    ws.send_json({"type": "auth", "token": TOKEN, "observe": observe})
    assert ws.receive_json()["type"] == "ready"


def test_observer_sees_tasks_and_can_answer_approvals(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "old.txt").write_text("x", encoding="utf-8")
    client = _client(
        tmp_path,
        [
            calls(("write_file", {"path": "old.txt", "content": "new", "overwrite": True})),
            text("Done."),
        ],
    )
    # One event loop for both sockets (as in the real server): `with client`.
    with client, client.websocket_connect(URL) as main, client.websocket_connect(URL) as overlay:
        _auth(main)
        _auth(overlay, observe=True)
        main.send_json({"type": "task.start", "goal": "replace old.txt"})

        # The overlay sees the task, including the approval request...
        seen = _collect_until(overlay, "approval.request")
        assert seen[0]["type"] == "task.started"
        req = seen[-1]
        assert _collect_until(main, "approval.request")[-1]["id"] == req["id"]

        # ...and can answer it; the main window is told to close its dialog.
        overlay.send_json({"type": "approval.response", "id": req["id"], "approved": True})
        resolved = _collect_until(main, "approval.resolved")
        assert resolved[-1]["id"] == req["id"]
        assert _collect_until(main, "task.finished")[-1]["status"] == "completed"
        assert _collect_until(overlay, "task.finished")[-1]["summary"] == "Done."
    assert (tmp_path / "docs" / "old.txt").read_text(encoding="utf-8") == "new"


def test_plain_clients_do_not_see_each_others_tasks(tmp_path: Path) -> None:
    client = _client(tmp_path, [text("Hello.")])
    with client, client.websocket_connect(URL) as a, client.websocket_connect(URL) as b:
        _auth(a)
        _auth(b)
        a.send_json({"type": "task.start", "goal": "hi"})
        assert _collect_until(a, "task.finished")[-1]["status"] == "completed"
        b.send_json({"type": "ping"})
        assert b.receive_json() == {"type": "pong"}  # nothing from a's task was queued


def test_unknown_approval_id_is_ignored(tmp_path: Path) -> None:
    client = _client(tmp_path, [text("x")])
    with client.websocket_connect(URL) as ws:
        _auth(ws)
        ws.send_json({"type": "approval.response", "id": "nope", "approved": True})
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}


# ======================================================================= process control


@pytest.fixture
def fake_exe(tmp_path: Path) -> Path:
    """A stand-in overlay: a tiny script that records its arguments and waits."""
    if sys.platform != "win32":
        pytest.skip("Windows only")
    script = tmp_path / "fake_overlay.py"
    script.write_text(
        "import sys, time, pathlib\n"
        "pathlib.Path(sys.argv[0]).with_suffix('.args').write_text(' '.join(sys.argv[1:]))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    bat = tmp_path / "jarvis-overlay.cmd"
    bat.write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
    return bat


def test_overlay_start_stop(fake_exe: Path, tmp_path: Path) -> None:
    o = Overlay(8765, "tok", exe=fake_exe)
    assert o.available and not o.running
    o.start()
    try:
        assert o.running
        o.start()  # idempotent
    finally:
        o.stop()
    assert not o.running


def test_overlay_url_and_discovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert overlay_url(8765, "abc") == "http://127.0.0.1:8765/ui/overlay.html#token=abc"
    exe = tmp_path / "jarvis-overlay.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setenv("JARVIS_OVERLAY_EXE", str(exe))
    assert find_overlay() == exe
    monkeypatch.delenv("JARVIS_OVERLAY_EXE")
    monkeypatch.setattr(overlay_mod.Path, "is_file", lambda self: False)
    assert find_overlay() is None
    with pytest.raises(OSError, match="isn't installed"):
        Overlay(1, "t").start()


class FakeOverlay:
    def __init__(self, available: bool = True) -> None:
        self.available = available
        self.running = False
        self.starts = 0

    def start(self) -> None:
        self.running = True
        self.starts += 1

    def stop(self) -> None:
        self.running = False


def test_hub_follows_the_setting(paths: AppPaths) -> None:
    hub = Hub(load_settings(), paths)
    fake = FakeOverlay()
    hub.overlay = fake  # type: ignore[assignment]
    client = TestClient(create_app(hub.settings, TOKEN, hub=hub), base_url="http://127.0.0.1")
    auth = {"Authorization": f"Bearer {TOKEN}"}

    status = client.get("/v1/status", headers=auth).json()["setup"]["overlay"]
    assert status == {
        "enabled": False, "available": True, "running": False,
        "interact_hotkey": "Ctrl+Alt+O", "hide_hotkey": "Ctrl+Alt+H",
    }  # fmt: skip
    r = client.post("/v1/overlay", json={"enabled": True}, headers=auth)
    assert r.status_code == 200 and r.json()["running"] and hub.settings.features.hud.enabled
    # Saved to config.toml, so it comes back on next start.
    assert (
        "enabled = true" in paths.config_file.read_text(encoding="utf-8").split("[features.hud]")[1]
    )
    client.put("/v1/settings", json={"features": {"hud": {"enabled": False}}}, headers=auth)
    assert not fake.running

    hub.overlay = FakeOverlay(available=False)  # type: ignore[assignment]
    r = client.post("/v1/overlay", json={"enabled": True}, headers=auth)
    assert r.status_code == 409 and "isn't available" in r.json()["detail"]


def test_headless_has_no_overlay_or_window(paths: AppPaths) -> None:
    hub = Hub(load_settings(), paths)
    client = TestClient(create_app(hub.settings, TOKEN, hub=hub), base_url="http://127.0.0.1")
    auth = {"Authorization": f"Bearer {TOKEN}"}
    assert client.post("/v1/window/show", headers=auth).json() == {"shown": False}
    assert client.post("/v1/overlay", json={"enabled": True}, headers=auth).status_code == 409
    assert client.post("/v1/window/show").status_code == 401


def test_window_show_calls_opener(paths: AppPaths) -> None:
    hub = Hub(load_settings(), paths)
    opened: list[int] = []
    hub.open_ui = lambda: opened.append(os.getpid())
    client = TestClient(create_app(hub.settings, TOKEN, hub=hub), base_url="http://127.0.0.1")
    r = client.post("/v1/window/show", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.json() == {"shown": True} and opened
