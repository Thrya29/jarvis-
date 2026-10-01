"""V2.2: live map data (aircraft, network, weather/agenda), map tools and API."""

from __future__ import annotations

import gzip
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx2 as httpx
import psutil
import pytest
from fastapi.testclient import TestClient

from jarvis.agent.factory import all_tools
from jarvis.core.config import Settings, load_settings
from jarvis.core.paths import AppPaths
from jarvis.llm.base import ToolCall
from jarvis.maps.network import CENTROIDS, GeoDB, snapshot
from jarvis.maps.service import MapService
from jarvis.maps.sky import bbox, describe, distance_km, parse_states
from jarvis.maps.weather import MapDataError, conditions, geocode
from jarvis.server.app import create_app
from jarvis.server.hub import Hub
from jarvis.tools.base import ToolRegistry
from jarvis.tools.maps import MAP_TOOLS
from tests.fakes import EventLog, make_ctx

TOKEN = "t" * 43
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def state(icao: str, lat: float | None, lon: float | None, squawk: str = "1200") -> list[Any]:
    return [icao, "TEST1  ", "India", 0, 0, lon, lat, 1000.0, False, 100.0, 90.0, 0.0,
            None, 1050.0, squawk, False, 0]  # fmt: skip


# ======================================================================= sky


def test_bbox_and_distance() -> None:
    box = bbox(12.97, 77.59, 111)
    assert box["lamin"] == pytest.approx(11.97, abs=0.01)
    assert box["lomax"] - box["lomin"] > 2  # wider in longitude away from the equator... a bit
    assert distance_km(0, 0, 0, 1) == pytest.approx(111.2, abs=0.2)


def test_parse_states_filters_sorts_and_flags_emergencies() -> None:
    data = {
        "states": [
            state("far", 15.5, 77.6),  # ~280 km: outside 150 km
            state("near", 13.0, 77.6),
            state("sos", 13.3, 77.6, squawk="7700"),
            state("nopos", None, None),
            ["broken"],
        ]
    }
    planes = parse_states(data, 12.97, 77.59, 150)
    assert [p.icao24 for p in planes] == ["near", "sos"]
    assert planes[0].callsign == "TEST1" and planes[0].altitude_m == 1050.0  # geo altitude
    assert planes[0].speed_kmh == pytest.approx(360)
    assert planes[1].alert == "emergency"
    text = describe(planes, 5)
    assert "2 aircraft" in text and "SQUAWK 7700 (emergency)" in text
    assert describe([], 5).startswith("No aircraft")


# ======================================================================= weather


async def test_geocode_prefers_country_hint() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.params["name"] == "Paris"
        return httpx.Response(
            200,
            json={
                "results": [
                    {"name": "Paris", "latitude": 48.85, "longitude": 2.35,
                     "country": "France", "population": 2_000_000},
                    {"name": "Paris", "latitude": 33.66, "longitude": -95.55,
                     "country": "United States", "admin1": "Texas", "population": 25_000},
                ]
            },
        )  # fmt: skip

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert (await geocode(http, "Paris")).country == "France"
        texas = await geocode(http, "Paris, Texas")
        assert texas.region == "Texas" and texas.label == "Paris, Texas, United States"


async def test_geocode_not_found() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"generationtime_ms": 0.1})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(MapDataError, match="couldn't find"):
            await geocode(http, "Nowhereville")


def test_weather_codes() -> None:
    assert conditions(0) == "clear sky" and conditions(95) == "thunderstorm"
    assert conditions(None) == "unknown"


# ======================================================================= network


def conn(status: str, laddr: tuple[str, int], raddr: tuple[str, int] | None, pid: int | None):
    addr = lambda a: SimpleNamespace(ip=a[0], port=a[1]) if a else ()  # noqa: E731
    return SimpleNamespace(status=status, laddr=addr(laddr), raddr=addr(raddr), pid=pid)


def test_snapshot_groups_filters_and_flags(tmp_path: Path) -> None:
    conns = [
        conn(psutil.CONN_ESTABLISHED, ("10.0.0.2", 50000), ("8.8.8.8", 443), None),
        conn(psutil.CONN_ESTABLISHED, ("10.0.0.2", 50001), ("8.8.8.8", 443), None),
        conn(psutil.CONN_ESTABLISHED, ("10.0.0.2", 50002), ("1.1.1.1", 6667), 4),
        conn(psutil.CONN_ESTABLISHED, ("10.0.0.2", 50003), ("192.168.1.5", 443), 4),  # LAN
        conn(psutil.CONN_ESTABLISHED, ("127.0.0.1", 50004), ("127.0.0.1", 8765), 4),
        conn(psutil.CONN_LISTEN, ("0.0.0.0", 6379), None, 4),  # noqa: S104
        conn(psutil.CONN_LISTEN, ("127.0.0.1", 8765), None, 4),  # loopback only: fine
        conn(psutil.CONN_TIME_WAIT, ("10.0.0.2", 50005), ("9.9.9.9", 443), None),
    ]
    snap = snapshot(GeoDB(tmp_path), conns)
    # Flagged peers first (both have one flag here), then by connection count.
    assert [p.ip for p in snap.peers] == ["8.8.8.8", "1.1.1.1"]
    by_ip = {p.ip: p for p in snap.peers}
    assert by_ip["8.8.8.8"].connections == 2 and by_ip["8.8.8.8"].ports == [443]
    assert by_ip["8.8.8.8"].flags == ["unknown program"]
    assert by_ip["1.1.1.1"].process == "System" and by_ip["1.1.1.1"].flags == ["unusual port"]
    assert [(e.port, e.process) for e in snap.listening] == [(6379, "System")]
    text = snap.summary()
    assert "Accepting connections from the network on: 6379" in text
    assert "isn't downloaded yet" in text


def test_geodb_download_verifies_and_looks_up(tmp_path: Path) -> None:
    fixture = Path(__file__).parent / "data" / "geo-test.mmdb"
    if not fixture.exists():
        pytest.skip("no test GeoIP database")
    geo = GeoDB(tmp_path)
    calls: list[str] = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        if len(calls) == 1:
            raise OSError("404")  # this month's file isn't published yet
        return gzip.compress(fixture.read_bytes())

    geo.download(fetch)
    assert len(calls) == 2 and geo.ready and not geo.stale
    hit = geo.lookup("8.8.8.8")
    assert hit is not None and hit["code"] == "US" and hit["lat"] == CENTROIDS["US"][0]
    assert geo.lookup("1.1.1.1") is None  # not in the database
    assert geo.lookup("not-an-ip") is None
    assert geo.lookup("49.36.1.1")["code"] == "IN"  # type: ignore[index]
    geo.close()


def test_geodb_rejects_garbage(tmp_path: Path) -> None:
    geo = GeoDB(tmp_path)
    with pytest.raises(OSError, match="invalid"):
        geo.download(lambda url: gzip.compress(b"not a database"))
    with pytest.raises(OSError, match="corrupt"):
        geo.download(lambda url: b"not gzip")
    assert not geo.ready


# ======================================================================= service + tools


def wx_and_sky(req: httpx.Request) -> httpx.Response:
    host = req.url.host
    if host == "opensky-network.org":
        return httpx.Response(200, json={"states": [state("abc123", 13.0, 77.6)]})
    if host == "api.open-meteo.com":
        return httpx.Response(
            200,
            json={
                "current": {"time": "2026-10-01T10:00", "temperature_2m": 27.4,
                            "apparent_temperature": 30.1, "relative_humidity_2m": 60,
                            "weather_code": 2, "wind_speed_10m": 8, "wind_direction_10m": 90,
                            "precipitation": 0, "is_day": 1},
                "hourly": {"time": ["2026-10-01T10:00", "2026-10-01T11:00"],
                           "temperature_2m": [27, 28], "precipitation_probability": [10, 70],
                           "weather_code": [2, 61]},
                "daily": {"time": ["2026-10-01"], "weather_code": [61],
                          "temperature_2m_max": [29], "temperature_2m_min": [20],
                          "precipitation_probability_max": [70],
                          "sunrise": ["2026-10-01T06:08"], "sunset": ["2026-10-01T18:09"]},
            },
        )  # fmt: skip
    if host == "geocoding-api.open-meteo.com":
        return httpx.Response(
            200, json={"results": [{"name": "Mumbai", "latitude": 19.07, "longitude": 72.88}]}
        )
    return httpx.Response(404)


def service(tmp_path: Path, home: bool = True) -> tuple[MapService, Settings]:
    settings = Settings(features={"maps": {"enabled": True}})  # type: ignore[arg-type]
    if home:
        settings.profile.location = "Bengaluru"
        settings.profile.latitude, settings.profile.longitude = 12.97, 77.59
    svc = MapService(lambda: settings, tmp_path, transport=httpx.MockTransport(wx_and_sky))
    return svc, settings


async def test_service_caches_sky(tmp_path: Path) -> None:
    svc, _ = service(tmp_path)
    first = await svc.sky_view()
    second = await svc.sky_view()
    assert first["aircraft"][0]["icao24"] == "abc123" and first == second
    assert first["source"].startswith("The OpenSky Network")


async def test_service_without_home_explains(tmp_path: Path) -> None:
    svc, _ = service(tmp_path, home=False)
    view = await svc.sky_view()
    assert "home location isn't set" in view["error"]


async def test_map_tools(tmp_path: Path) -> None:
    svc, _ = service(tmp_path)
    ctx = make_ctx(tmp_path / "root")
    events = EventLog()
    ctx.emit = events
    ctx.maps = svc
    reg = ToolRegistry(MAP_TOOLS)

    out = await reg.execute(ToolCall("1", "map_show", {"view": "sky", "place": "Mumbai"}), ctx)
    assert not out.is_error and "Mumbai" in out.content
    assert events.events[-1] == {
        "type": "map.focus", "view": "sky", "lat": 19.07, "lon": 72.88, "label": "Mumbai",
    }  # fmt: skip

    out = await reg.execute(ToolCall("2", "flights_nearby", {}), ctx)
    assert "TEST1" in out.content and "<untrusted_content" in out.content
    out = await reg.execute(ToolCall("3", "weather", {}), ctx)
    assert "partly cloudy, 27°C" in out.content and "Rain likely from about 11:00" in out.content
    out = await reg.execute(ToolCall("4", "network_activity", {"limit": 3}), ctx)
    assert not out.is_error

    ctx.maps = None
    out = await reg.execute(ToolCall("5", "weather", {}), ctx)
    assert out.is_error and "turned off" in out.content
    svc.close()


def test_map_tools_only_when_enabled() -> None:
    assert "flights_nearby" not in [t.name for t in all_tools()]
    assert {"map_show", "flights_nearby", "network_activity", "weather"} <= {
        t.name for t in all_tools(maps=True)
    }


# ======================================================================= API


@pytest.fixture
def hub(paths: AppPaths) -> Hub:
    return Hub(load_settings(), paths)


@pytest.fixture
def client(hub: Hub) -> TestClient:
    return TestClient(create_app(hub.settings, TOKEN, hub=hub), base_url="http://127.0.0.1")


def test_maps_api_and_home_location(client: TestClient, hub: Hub, tmp_path: Path) -> None:
    assert client.get("/v1/maps/sky", headers=AUTH).status_code == 409  # feature off
    r = client.put("/v1/settings", json={"features": {"maps": {"enabled": True}}}, headers=AUTH)
    assert r.status_code == 200
    hub.maps._transport = httpx.MockTransport(wx_and_sky)

    r = client.put("/v1/settings", json={"profile": {"location": "Mumbai"}}, headers=AUTH)
    assert r.status_code == 200, r.text
    p = hub.settings.profile
    assert (p.location, p.latitude, p.longitude) == ("Mumbai", 19.07, 72.88)

    sky = client.get("/v1/maps/sky", headers=AUTH).json()
    assert sky["home"]["name"] == "Mumbai" and sky["radius_km"] == 150
    wx = client.get("/v1/maps/situation", headers=AUTH).json()
    assert wx["weather"]["conditions"] == "partly cloudy" and wx["events"] == []
    assert client.get("/v1/maps/locate?q=Mumbai", headers=AUTH).json()["lat"] == 19.07

    client.put("/v1/settings", json={"features": {"maps": {"sky": False}}}, headers=AUTH)
    assert client.get("/v1/maps/sky", headers=AUTH).status_code == 409

    r = client.put("/v1/settings", json={"profile": {"location": ""}}, headers=AUTH)
    assert r.status_code == 200 and hub.settings.profile.latitude is None
