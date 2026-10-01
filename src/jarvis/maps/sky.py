"""Aircraft around the user from the OpenSky Network's public ADS-B data.

Anonymous access is free but limited (a daily credit allowance, 10-second resolution),
so results are cached and the UI only polls while the Sky panel is on screen.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

import httpx2 as httpx

from jarvis.maps.weather import MapDataError

STATES_URL = "https://opensky-network.org/api/states/all"
EMERGENCY_SQUAWKS = {"7500": "hijack", "7600": "radio failure", "7700": "emergency"}
EARTH_KM = 6371.0


@dataclass
class Aircraft:
    icao24: str
    callsign: str
    country: str
    lat: float
    lon: float
    altitude_m: float | None
    speed_kmh: float | None
    heading: float | None
    vertical_ms: float | None
    on_ground: bool
    squawk: str
    distance_km: float
    alert: str = ""  # emergency squawk meaning, if any

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_KM * math.asin(math.sqrt(a))


def bbox(lat: float, lon: float, radius_km: float) -> dict[str, float]:
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * max(0.1, math.cos(math.radians(lat))))
    return {
        "lamin": round(max(-90.0, lat - dlat), 4),
        "lamax": round(min(90.0, lat + dlat), 4),
        "lomin": round(max(-180.0, lon - dlon), 4),
        "lomax": round(min(180.0, lon + dlon), 4),
    }


def parse_states(data: dict[str, Any], lat: float, lon: float, radius_km: float) -> list[Aircraft]:
    out: list[Aircraft] = []
    for s in data.get("states") or []:
        try:
            a_lon, a_lat = s[5], s[6]
            if a_lat is None or a_lon is None:
                continue
            dist = distance_km(lat, lon, a_lat, a_lon)
            if dist > radius_km:
                continue
            squawk = (s[14] or "").strip()
            altitude = s[13] if s[13] is not None else s[7]
            out.append(
                Aircraft(
                    icao24=str(s[0]),
                    callsign=(s[1] or "").strip() or str(s[0]).upper(),
                    country=s[2] or "",
                    lat=float(a_lat),
                    lon=float(a_lon),
                    altitude_m=float(altitude) if altitude is not None else None,
                    speed_kmh=float(s[9]) * 3.6 if s[9] is not None else None,
                    heading=float(s[10]) if s[10] is not None else None,
                    vertical_ms=float(s[11]) if s[11] is not None else None,
                    on_ground=bool(s[8]),
                    squawk=squawk,
                    distance_km=round(dist, 1),
                    alert=EMERGENCY_SQUAWKS.get(squawk, ""),
                )
            )
        except (IndexError, TypeError, ValueError):
            continue  # one malformed vector shouldn't drop the rest
    out.sort(key=lambda a: a.distance_km)
    return out


async def fetch_aircraft(
    http: httpx.AsyncClient, lat: float, lon: float, radius_km: float
) -> list[Aircraft]:
    try:
        resp = await http.get(STATES_URL, params=bbox(lat, lon, radius_km), timeout=25)
    except httpx.HTTPError as exc:
        raise MapDataError(f"couldn't reach the OpenSky Network: {exc}") from exc
    if resp.status_code == 429:
        raise MapDataError("OpenSky's free daily allowance is used up; flights resume later")
    if resp.status_code >= 400:
        raise MapDataError(f"OpenSky error {resp.status_code}")
    return parse_states(resp.json(), lat, lon, radius_km)


def describe(aircraft: list[Aircraft], limit: int) -> str:
    if not aircraft:
        return "No aircraft are reporting positions nearby right now."
    lines = []
    for a in aircraft[:limit]:
        alt = (
            "on the ground"
            if a.on_ground
            else (f"{a.altitude_m:,.0f} m" if a.altitude_m is not None else "altitude unknown")
        )
        speed = f", {a.speed_kmh:.0f} km/h" if a.speed_kmh else ""
        head = f", heading {a.heading:.0f}°" if a.heading is not None else ""
        alert = f" — SQUAWK {a.squawk} ({a.alert})" if a.alert else ""
        lines.append(
            f"- {a.callsign} ({a.country}): {a.distance_km} km away, {alt}{speed}{head}{alert}"
        )
    return f"{len(aircraft)} aircraft within range; nearest first:\n" + "\n".join(lines)
