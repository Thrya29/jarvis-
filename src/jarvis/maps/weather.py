"""Place lookup and weather from Open-Meteo (free, no API key, no account)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx2 as httpx

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# WMO weather interpretation codes, as used by Open-Meteo.
WMO = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "heavy freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "heavy freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "violent showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "severe thunderstorm with hail",
}  # fmt: skip


class MapDataError(RuntimeError):
    """A map data source failed; the message is safe to show to the user."""


@dataclass(frozen=True)
class Place:
    name: str
    latitude: float
    longitude: float
    country: str = ""
    region: str = ""
    timezone: str = ""

    @property
    def label(self) -> str:
        return ", ".join(p for p in (self.name, self.region, self.country) if p)


@dataclass
class Weather:
    place: str
    time: str
    temperature_c: float
    feels_like_c: float
    humidity_pct: float
    wind_kmh: float
    wind_dir_deg: float
    precipitation_mm: float
    conditions: str
    is_day: bool
    hours: list[dict[str, Any]] = field(default_factory=list)  # next 12 h
    days: list[dict[str, Any]] = field(default_factory=list)  # today + 2

    def summary(self) -> str:
        lines = [
            f"{self.place}: {self.conditions}, {self.temperature_c:.0f}°C "
            f"(feels like {self.feels_like_c:.0f}°C), humidity {self.humidity_pct:.0f}%, "
            f"wind {self.wind_kmh:.0f} km/h."
        ]
        wet = [h for h in self.hours if (h.get("rain_pct") or 0) >= 50]
        if wet:
            lines.append(
                f"Rain likely from about {wet[0]['time'][-5:]} (chance {wet[0]['rain_pct']}%)."
            )
        for d in self.days:
            lines.append(
                f"{d['date']}: {d['conditions']}, {d['min_c']:.0f}-{d['max_c']:.0f}°C, "
                f"rain chance {d['rain_pct']}%, "
                f"sunrise {d['sunrise'][-5:]}, sunset {d['sunset'][-5:]}."
            )
        return "\n".join(lines)


def conditions(code: Any) -> str:
    try:
        return WMO.get(int(code), "unknown")
    except (TypeError, ValueError):
        return "unknown"


async def _get(http: httpx.AsyncClient, url: str, params: dict[str, Any], what: str) -> Any:
    try:
        resp = await http.get(url, params=params, timeout=20)
    except httpx.HTTPError as exc:
        raise MapDataError(f"couldn't reach {what}: {exc}") from exc
    if resp.status_code == 429:
        raise MapDataError(f"{what} is rate-limiting requests; try again in a minute")
    if resp.status_code >= 400:
        raise MapDataError(f"{what} error {resp.status_code}")
    return resp.json()


async def geocode(http: httpx.AsyncClient, query: str) -> Place:
    """Best match for 'City' or 'City, Region/Country'."""
    parts = [p.strip() for p in query.split(",") if p.strip()]
    if not parts:
        raise MapDataError("enter a place name")
    data = await _get(
        http, GEOCODE_URL,
        {"name": parts[0], "count": 10, "language": "en", "format": "json"},
        "the place search",
    )  # fmt: skip
    results = data.get("results") or []
    if not results:
        raise MapDataError(f"couldn't find {query!r}; try 'City, Country'")
    hint = " ".join(parts[1:]).lower()

    def score(r: dict[str, Any]) -> tuple[int, float]:
        text = " ".join(str(r.get(k, "")) for k in ("country", "country_code", "admin1")).lower()
        matched = int(bool(hint) and any(w in text for w in hint.split()))
        return matched, float(r.get("population") or 0)

    best = max(results, key=score)
    return Place(
        best["name"],
        float(best["latitude"]),
        float(best["longitude"]),
        best.get("country", ""),
        best.get("admin1", ""),
        best.get("timezone", ""),
    )


async def forecast(http: httpx.AsyncClient, lat: float, lon: float, place: str) -> Weather:
    data = await _get(
        http,
        FORECAST_URL,
        {
            "latitude": f"{lat:.4f}",
            "longitude": f"{lon:.4f}",
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,"
            "wind_speed_10m,wind_direction_10m,precipitation,is_day",
            "hourly": "temperature_2m,precipitation_probability,weather_code",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,sunrise,sunset,"
            "precipitation_probability_max",
            "forecast_days": 3,
            "timezone": "auto",
        },
        "the weather service",
    )
    cur = data.get("current") or {}
    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    now = cur.get("time", "")
    start = next((i for i, t in enumerate(times) if t >= now[:13]), 0)
    hours = [
        {
            "time": times[i],
            "temp_c": hourly["temperature_2m"][i],
            "rain_pct": hourly["precipitation_probability"][i],
            "conditions": conditions(hourly["weather_code"][i]),
        }
        for i in range(start, min(start + 12, len(times)))
    ]
    daily = data.get("daily") or {}
    days = [
        {
            "date": d,
            "conditions": conditions(daily["weather_code"][i]),
            "max_c": daily["temperature_2m_max"][i],
            "min_c": daily["temperature_2m_min"][i],
            "rain_pct": daily["precipitation_probability_max"][i],
            "sunrise": daily["sunrise"][i],
            "sunset": daily["sunset"][i],
        }
        for i, d in enumerate(daily.get("time") or [])
    ]
    return Weather(
        place,
        now,
        float(cur.get("temperature_2m") or 0),
        float(cur.get("apparent_temperature") or 0),
        float(cur.get("relative_humidity_2m") or 0),
        float(cur.get("wind_speed_10m") or 0),
        float(cur.get("wind_direction_10m") or 0),
        float(cur.get("precipitation") or 0),
        conditions(cur.get("weather_code")),
        bool(cur.get("is_day", 1)),
        hours,
        days,
    )
