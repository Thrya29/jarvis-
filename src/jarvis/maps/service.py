"""Map data for the UI panels and the agent's map tools, cached per source."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2 as httpx

from jarvis.core.config import Settings
from jarvis.maps.network import GeoDB, Snapshot, snapshot
from jarvis.maps.sky import Aircraft, fetch_aircraft
from jarvis.maps.weather import MapDataError, Place, Weather, forecast, geocode

log = logging.getLogger(__name__)

SKY_TTL_S = 60.0  # OpenSky anonymous data only updates every 10 s and is credit-limited
WEATHER_TTL_S = 600.0
EVENTS_TTL_S = 300.0
NETWORK_TTL_S = 4.0
ONLINE_MEETING = ("http", "teams", "zoom", "meet.google", "webex", "skype", "online")


class MapService:
    def __init__(
        self,
        settings: Callable[[], Settings],
        data_dir: Path,
        connections: Callable[[], Any] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._connections = connections
        self._transport = transport
        self.geo = GeoDB(data_dir / "geo")
        self._cache: dict[str, tuple[float, Any]] = {}
        self._places: dict[str, Place | None] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._geo_task: asyncio.Task[None] | None = None
        self.geo_error = ""

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self._transport, headers={"User-Agent": "JARVIS"})

    async def _cached(self, key: str, ttl: float, make: Callable[[], Awaitable[Any]]) -> Any:
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:  # concurrent callers share one upstream request
            hit = self._cache.get(key)
            if hit and time.time() - hit[0] < ttl:
                return hit[1]
            value = await make()
            self._cache[key] = (time.time(), value)
            return value

    # ---------------------------------------------------------------- places
    def home(self) -> Place | None:
        p = self._settings().profile
        if p.latitude is None or p.longitude is None:
            return None
        return Place(p.location or "Home", p.latitude, p.longitude)

    def require_home(self) -> Place:
        home = self.home()
        if home is None:
            raise MapDataError(
                "your home location isn't set; add your city in Settings → About you"
            )
        return home

    async def locate(self, query: str) -> Place:
        key = query.strip().lower()
        if key not in self._places:
            async with self._http() as http:
                try:
                    self._places[key] = await geocode(http, query)
                except MapDataError:
                    self._places[key] = None
                    raise
        place = self._places[key]
        if place is None:
            raise MapDataError(f"couldn't find {query!r}; try 'City, Country'")
        return place

    # ---------------------------------------------------------------- sky
    async def aircraft(self) -> tuple[Place, list[Aircraft]]:
        home = self.require_home()
        radius = self._settings().features.maps.sky_radius_km
        key = f"sky:{home.latitude:.2f}:{home.longitude:.2f}:{radius}"

        async def fetch() -> list[Aircraft]:
            async with self._http() as http:
                return await fetch_aircraft(http, home.latitude, home.longitude, radius)

        return home, await self._cached(key, SKY_TTL_S, fetch)

    async def sky_view(self) -> dict[str, Any]:
        radius = self._settings().features.maps.sky_radius_km
        try:
            home, planes = await self.aircraft()
        except MapDataError as exc:
            return {"error": str(exc), "home": _place(self.home()), "radius_km": radius}
        return {
            "home": _place(home),
            "radius_km": radius,
            "aircraft": [a.to_dict() for a in planes],
            "fetched_at": self._cache_time("sky:"),
            "source": "The OpenSky Network (opensky-network.org)",
        }

    # ---------------------------------------------------------------- network
    def _start_geo_download(self) -> None:
        if self._geo_task is not None and not self._geo_task.done():
            return

        def fetch(url: str) -> bytes:
            try:
                with httpx.Client(timeout=60, follow_redirects=True) as c:
                    resp = c.get(url)
            except httpx.HTTPError as exc:
                raise OSError(str(exc)) from exc
            if resp.status_code != 200:
                raise OSError(f"HTTP {resp.status_code}")
            return resp.content

        async def run() -> None:
            try:
                await asyncio.to_thread(self.geo.download, fetch)
                self.geo_error = ""
            except OSError as exc:
                self.geo_error = str(exc)
                log.warning("geolocation database download failed: %s", exc)

        self._geo_task = asyncio.ensure_future(run())

    async def network(self) -> Snapshot:
        if self.geo.stale:
            self._start_geo_download()
        result: Snapshot = await self._cached(
            "network", NETWORK_TTL_S, lambda: asyncio.to_thread(snapshot, self.geo)
        )
        return result

    async def network_view(self) -> dict[str, Any]:
        snap = await self.network()
        return {
            "home": _place(self.home()),
            **snap.to_dict(),
            "geo_downloading": self._geo_task is not None and not self._geo_task.done(),
            "geo_error": self.geo_error,
            "source": "IP Geolocation by DB-IP (db-ip.com), CC BY 4.0",
        }

    # ---------------------------------------------------------------- situation
    async def weather(self, place: Place | None = None) -> Weather:
        where = place or self.require_home()
        key = f"wx:{where.latitude:.2f}:{where.longitude:.2f}"

        async def fetch() -> Weather:
            async with self._http() as http:
                return await forecast(http, where.latitude, where.longitude, where.label)

        result: Weather = await self._cached(key, WEATHER_TTL_S, fetch)
        return result

    async def _events(self) -> list[dict[str, Any]]:
        from jarvis.connect.accounts import AccountError, Capability
        from jarvis.connect.services import ServiceError

        manager = self._connections() if self._connections else None
        if manager is None:
            return []
        start = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        out: list[dict[str, Any]] = []
        for account in manager.accounts():
            if not account.can(Capability.CALENDAR_READ):
                continue
            try:
                async with manager.open(Capability.CALENDAR_READ, account.email) as (_, svc):
                    events = await svc.events(start, end, 25)
            except (AccountError, ServiceError) as exc:
                log.info("calendar for the situation map unavailable: %s", exc)
                continue
            for e in events:
                item = {
                    "subject": e.subject,
                    "start": e.start,
                    "end": e.end,
                    "location": e.location,
                    "account": account.email,
                }
                spot = await self._event_place(e.location)
                if spot is not None:
                    item.update(lat=spot.latitude, lon=spot.longitude, place=spot.label)
                out.append(item)
        return out

    async def _event_place(self, location: str) -> Place | None:
        """Approximate (city-level) position of an event, when its location names a place."""
        text = location.strip()
        if not text or any(m in text.lower() for m in ONLINE_MEETING):
            return None
        candidate = text.split(",")[-1].strip() if "," in text else text
        with contextlib.suppress(MapDataError):
            return await self.locate(candidate)
        return None

    async def situation_view(self) -> dict[str, Any]:
        out: dict[str, Any] = {"home": _place(self.home())}
        try:
            out["weather"] = asdict(await self.weather())
        except MapDataError as exc:
            out["weather_error"] = str(exc)
        try:
            out["events"] = await self._cached("events", EVENTS_TTL_S, self._events)
        except Exception as exc:  # the agenda is optional; never break the panel
            log.warning("situation events failed: %s", exc)
            out["events"] = []
        out["source"] = "Weather: Open-Meteo.com (CC BY 4.0)"
        return out

    def _cache_time(self, prefix: str) -> float | None:
        times = [t for k, (t, _) in self._cache.items() if k.startswith(prefix)]
        return max(times) if times else None

    def close(self) -> None:
        if self._geo_task is not None:
            self._geo_task.cancel()
        self.geo.close()


def _place(p: Place | None) -> dict[str, Any] | None:
    if p is None:
        return None
    return {"name": p.label, "lat": p.latitude, "lon": p.longitude}
