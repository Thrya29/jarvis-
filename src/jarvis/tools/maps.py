"""Map tools: show places on the map panels, and answer from live map data."""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import Field

from jarvis.maps.service import MapService
from jarvis.maps.sky import describe
from jarvis.maps.weather import MapDataError
from jarvis.safety.policy import Risk
from jarvis.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult


def _maps(ctx: ToolContext) -> MapService:
    if ctx.maps is None:
        raise ToolError("Live maps are turned off; enable them in Settings → Features.")
    service: MapService = ctx.maps
    return service


class MapShowArgs(ToolArgs):
    view: Literal["sky", "network", "situation"] = Field(
        description="sky = aircraft overhead, network = this PC's connections, "
        "situation = weather and today's agenda."
    )
    place: str | None = Field(
        default=None, max_length=100, description="Optional place to centre on, e.g. 'Mumbai'."
    )


class MapShow(Tool[MapShowArgs]):
    name = "map_show"
    description = (
        "Open one of the live map panels in the JARVIS window, optionally centred on a place."
    )
    args_model: ClassVar[type[ToolArgs]] = MapShowArgs
    risk = Risk.READ

    async def run(self, args: MapShowArgs, ctx: ToolContext) -> ToolResult:
        maps = _maps(ctx)
        event: dict[str, Any] = {"type": "map.focus", "view": args.view}
        label = "home"
        try:
            place = await maps.locate(args.place) if args.place else maps.home()
        except MapDataError as exc:
            raise ToolError(str(exc)) from exc
        if place is not None:
            event.update(lat=place.latitude, lon=place.longitude, label=place.label)
            label = place.label
        await ctx.emit(event)
        return ToolResult(f"Showing the {args.view} map ({label}) in the JARVIS window.")


class FlightsArgs(ToolArgs):
    limit: int = Field(default=8, ge=1, le=30)


class FlightsNearby(Tool[FlightsArgs]):
    name = "flights_nearby"
    description = (
        "Aircraft currently flying near the user's home (live ADS-B data from the OpenSky "
        "Network): callsign, distance, altitude, speed, heading, and any emergency squawk."
    )
    args_model: ClassVar[type[ToolArgs]] = FlightsArgs
    risk = Risk.READ

    async def run(self, args: FlightsArgs, ctx: ToolContext) -> ToolResult:
        try:
            _, planes = await _maps(ctx).aircraft()
        except MapDataError as exc:
            raise ToolError(str(exc)) from exc
        # Callsigns and countries are broadcast by aircraft: data, not instructions.
        return ToolResult(describe(planes, args.limit), untrusted=True, source="OpenSky Network")


class NetworkArgs(ToolArgs):
    limit: int = Field(default=15, ge=1, le=50)


class NetworkActivity(Tool[NetworkArgs]):
    name = "network_activity"
    description = (
        "Which programs on this PC are connected to the internet, to which addresses and "
        "countries, anything unusual (odd ports, unknown programs), and which ports accept "
        "connections from the network. Read-only; changes nothing."
    )
    args_model: ClassVar[type[ToolArgs]] = NetworkArgs
    risk = Risk.READ

    async def run(self, args: NetworkArgs, ctx: ToolContext) -> ToolResult:
        snap = await _maps(ctx).network()
        return ToolResult(snap.summary(args.limit))


class WeatherArgs(ToolArgs):
    place: str | None = Field(
        default=None, max_length=100, description="City; omit for the user's home."
    )


class WeatherNow(Tool[WeatherArgs]):
    name = "weather"
    description = "Current weather and a 3-day forecast (Open-Meteo) for home or a named city."
    args_model: ClassVar[type[ToolArgs]] = WeatherArgs
    risk = Risk.READ

    async def run(self, args: WeatherArgs, ctx: ToolContext) -> ToolResult:
        maps = _maps(ctx)
        try:
            place = await maps.locate(args.place) if args.place else None
            report = await maps.weather(place)
        except MapDataError as exc:
            raise ToolError(str(exc)) from exc
        return ToolResult(report.summary())


MAP_TOOLS: list[Tool[Any]] = [MapShow(), FlightsNearby(), NetworkActivity(), WeatherNow()]
