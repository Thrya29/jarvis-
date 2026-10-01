import { Map as MapLibre, NavigationControl, Popup, setWorkerUrl, type GeoJSONSource, type Map as MLMap } from "maplibre-gl";
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, errorText, put } from "../lib/api";
import type { NetworkView, SettingsView, SituationView, SkyView } from "../lib/types";
import { arc, circle, HUD_STYLE, planeImage, zoomForRadius } from "../maps/style";
import { useApp } from "../state";

// The worker is a same-origin module file, so the CSP needs no blob: workers.
setWorkerUrl(workerUrl);

type Panel = "sky" | "network" | "situation";
const PANELS: { id: Panel; label: string }[] = [
  { id: "sky", label: "Live sky" },
  { id: "network", label: "Network" },
  { id: "situation", label: "Situation" },
];
const POLL_MS: Record<Panel, number> = { sky: 90_000, network: 5_000, situation: 600_000 };
const LAYERS: Record<Panel, string[]> = {
  sky: ["sky-rings", "sky-planes", "sky-labels"],
  network: ["net-arcs", "net-countries", "net-country-labels"],
  situation: ["sit-events", "sit-event-labels"],
};
const EMPTY: GeoJSON.FeatureCollection = { type: "FeatureCollection", features: [] };

function setData(map: MLMap, source: string, data: GeoJSON.FeatureCollection) {
  (map.getSource(source) as GeoJSONSource | undefined)?.setData(data);
}

function addLayers(map: MLMap) {
  const plane = planeImage();
  if (!map.hasImage("plane")) map.addImage("plane", plane, { sdf: true, pixelRatio: 2 });
  for (const id of ["home", "sky-rings", "sky", "net-arcs", "net-countries", "sit-events"]) {
    map.addSource(id, { type: "geojson", data: EMPTY });
  }
  map.addLayer({
    id: "sky-rings",
    type: "line",
    source: "sky-rings",
    paint: { "line-color": "#00e5ff", "line-opacity": 0.35, "line-width": 1, "line-dasharray": [4, 4] },
  });
  map.addLayer({
    id: "net-arcs",
    type: "line",
    source: "net-arcs",
    layout: { "line-cap": "round" },
    paint: {
      "line-color": ["case", ["get", "flagged"], "#ffb020", "#00e5ff"],
      "line-opacity": 0.55,
      "line-width": ["interpolate", ["linear"], ["get", "count"], 1, 1, 20, 4],
    },
  });
  map.addLayer({
    id: "home",
    type: "circle",
    source: "home",
    paint: {
      "circle-radius": 6,
      "circle-color": "#00e5ff",
      "circle-stroke-color": "#d6f6ff",
      "circle-stroke-width": 2,
      "circle-blur": 0.1,
    },
  });
  map.addLayer({
    id: "sky-planes",
    type: "symbol",
    source: "sky",
    layout: {
      "icon-image": "plane",
      "icon-size": ["interpolate", ["linear"], ["zoom"], 4, 0.55, 10, 0.95],
      "icon-rotate": ["coalesce", ["get", "heading"], 0],
      "icon-rotation-alignment": "map",
      "icon-allow-overlap": true,
    },
    paint: {
      "icon-color": [
        "case",
        ["!=", ["get", "alert"], ""],
        "#ff3b5c",
        ["get", "on_ground"],
        "#6f93a6",
        ["interpolate", ["linear"], ["coalesce", ["get", "altitude_m"], 0], 0, "#3dffa8", 3000, "#00e5ff", 10000, "#8fb4ff"],
      ],
      "icon-halo-color": "#020a12",
      "icon-halo-width": 1,
    },
  });
  map.addLayer({
    id: "sky-labels",
    type: "symbol",
    source: "sky",
    minzoom: 6,
    layout: {
      "text-field": ["get", "callsign"],
      "text-font": ["Noto Sans Regular"],
      "text-size": 10,
      "text-offset": [0, 1.5],
      "text-anchor": "top",
    },
    paint: { "text-color": "#8fd8ec", "text-halo-color": "#020a12", "text-halo-width": 1 },
  });
  map.addLayer({
    id: "net-countries",
    type: "circle",
    source: "net-countries",
    paint: {
      "circle-radius": ["interpolate", ["linear"], ["get", "count"], 1, 5, 30, 14],
      "circle-color": ["case", ["get", "flagged"], "#ffb020", "#00e5ff"],
      "circle-opacity": 0.35,
      "circle-stroke-color": ["case", ["get", "flagged"], "#ffb020", "#00e5ff"],
      "circle-stroke-width": 1.5,
    },
  });
  map.addLayer({
    id: "net-country-labels",
    type: "symbol",
    source: "net-countries",
    layout: {
      "text-field": ["concat", ["get", "name"], " · ", ["to-string", ["get", "count"]]],
      "text-font": ["Noto Sans Regular"],
      "text-size": 11,
      "text-offset": [0, 1.4],
      "text-anchor": "top",
    },
    paint: { "text-color": "#d6f6ff", "text-halo-color": "#020a12", "text-halo-width": 1.2 },
  });
  map.addLayer({
    id: "sit-events",
    type: "circle",
    source: "sit-events",
    paint: {
      "circle-radius": 7,
      "circle-color": "#ffb020",
      "circle-opacity": 0.5,
      "circle-stroke-color": "#ffb020",
      "circle-stroke-width": 2,
    },
  });
  map.addLayer({
    id: "sit-event-labels",
    type: "symbol",
    source: "sit-events",
    layout: {
      "text-field": ["get", "subject"],
      "text-font": ["Noto Sans Regular"],
      "text-size": 11,
      "text-offset": [0, 1.3],
      "text-anchor": "top",
      "text-allow-overlap": true,
    },
    paint: { "text-color": "#ffd58a", "text-halo-color": "#020a12", "text-halo-width": 1.2 },
  });
}

function showPanel(map: MLMap, panel: Panel) {
  for (const [p, ids] of Object.entries(LAYERS)) {
    for (const id of ids) map.setLayoutProperty(id, "visibility", p === panel ? "visible" : "none");
  }
}

/** Popup content built with textContent only. */
function popup(lines: [string, string][]): HTMLElement {
  const box = document.createElement("div");
  for (const [k, v] of lines) {
    const row = document.createElement("div");
    const key = document.createElement("span");
    key.className = "hud-label";
    key.textContent = `${k} `;
    const val = document.createElement("span");
    val.textContent = v;
    row.append(key, val);
    box.append(row);
  }
  return box;
}

const fmt = (n: number | null | undefined, unit: string, digits = 0) =>
  n === null || n === undefined ? "—" : `${n.toLocaleString(undefined, { maximumFractionDigits: digits })} ${unit}`;

function SkyInfo({ data }: { data: SkyView | null }) {
  if (!data) return <p className="hint">Loading aircraft…</p>;
  if (data.error) return <p className="text-amber">{data.error}</p>;
  const planes = data.aircraft || [];
  const alerts = planes.filter((p) => p.alert);
  return (
    <>
      <p className="m-0 font-[family-name:var(--font-hud)] text-3xl font-bold text-cyan">{planes.length}</p>
      <p className="hud-label mt-0">aircraft within {data.radius_km} km</p>
      {alerts.map((a) => (
        <p key={a.icao24} className="text-danger">
          ⚠ {a.callsign}: squawk {a.squawk} ({a.alert})
        </p>
      ))}
      <ul className="m-0 mt-2 flex list-none flex-col gap-1 p-0 font-mono text-xs">
        {planes.slice(0, 12).map((p) => (
          <li key={p.icao24} className="flex justify-between gap-2">
            <span className={p.alert ? "text-danger" : "text-ice"}>{p.callsign}</span>
            <span className="text-muted">
              {p.on_ground ? "ground" : fmt(p.altitude_m, "m")} · {p.distance_km} km
            </span>
          </li>
        ))}
      </ul>
      <p className="hint mt-3 text-xs">Live ADS-B: {data.source}. Refreshes every 90 s.</p>
    </>
  );
}

function NetworkInfo({ data }: { data: NetworkView | null }) {
  if (!data) return <p className="hint">Reading connections…</p>;
  const flagged = data.peers.filter((p) => p.flags.length);
  return (
    <>
      <p className="m-0 font-[family-name:var(--font-hud)] text-3xl font-bold text-cyan">{data.peers.length}</p>
      <p className="hud-label mt-0">outside addresses connected</p>
      {data.geo_downloading ? <p className="hint">Downloading the location database (~4 MB)…</p> : null}
      {data.geo_error ? <p className="text-amber">Locations unavailable: {data.geo_error}</p> : null}
      {flagged.length ? (
        <>
          <h3 className="hud-title mt-3 text-amber">Worth a look</h3>
          <ul className="m-0 flex list-none flex-col gap-1 p-0 text-xs">
            {flagged.slice(0, 8).map((p) => (
              <li key={`${p.ip}-${p.pid}`}>
                <span className="text-amber">{p.process || "unknown program"}</span> → {p.ip}:{p.ports.join(",")}{" "}
                <span className="text-muted">({p.flags.join(", ")})</span>
              </li>
            ))}
          </ul>
        </>
      ) : null}
      <h3 className="hud-title mt-3">Top programs</h3>
      <ul className="m-0 flex list-none flex-col gap-1 p-0 font-mono text-xs">
        {Object.entries(
          data.peers.reduce<Record<string, number>>((acc, p) => {
            const k = p.process || "unknown";
            acc[k] = (acc[k] || 0) + p.connections;
            return acc;
          }, {}),
        )
          .sort((a, b) => b[1] - a[1])
          .slice(0, 8)
          .map(([name, n]) => (
            <li key={name} className="flex justify-between">
              <span>{name}</span>
              <span className="text-muted">{n}</span>
            </li>
          ))}
      </ul>
      {data.listening.length ? (
        <>
          <h3 className="hud-title mt-3">Open to the network</h3>
          <p className="m-0 font-mono text-xs text-muted">
            {data.listening.slice(0, 14).map((e) => `${e.port} ${e.process || "?"}`).join(" · ")}
          </p>
        </>
      ) : null}
      <p className="hint mt-3 text-xs">Looked up on this PC. {data.source}.</p>
    </>
  );
}

function SituationInfo({ data }: { data: SituationView | null }) {
  if (!data) return <p className="hint">Loading weather…</p>;
  const w = data.weather;
  return (
    <>
      {w ? (
        <>
          <p className="hud-label m-0">{w.place}</p>
          <p className="m-0 font-[family-name:var(--font-hud)] text-4xl font-bold text-cyan">{Math.round(w.temperature_c)}°C</p>
          <p className="m-0 capitalize">{w.conditions}</p>
          <p className="hint m-0 text-xs">
            Feels {Math.round(w.feels_like_c)}°C · humidity {Math.round(w.humidity_pct)}% · wind {Math.round(w.wind_kmh)} km/h
          </p>
          <div className="mt-3 flex h-14 items-end gap-0.5" aria-label="Chance of rain, next 12 hours">
            {w.hours.map((h) => (
              <div key={h.time} className="flex flex-1 flex-col items-center gap-0.5" title={`${h.time.slice(11)} · ${h.rain_pct}% rain · ${h.temp_c}°C`}>
                <div className="w-full rounded-sm bg-cyan/60" style={{ height: `${Math.max(2, h.rain_pct * 0.4)}px` }} />
                <span className="font-mono text-[9px] text-muted">{h.time.slice(11, 13)}</span>
              </div>
            ))}
          </div>
          <p className="hud-label mt-0.5">rain chance, next 12 h</p>
          <ul className="m-0 mt-2 flex list-none flex-col gap-0.5 p-0 text-xs">
            {w.days.map((d) => (
              <li key={d.date} className="flex justify-between gap-2">
                <span>{new Date(d.date).toLocaleDateString(undefined, { weekday: "short" })}</span>
                <span className="capitalize text-muted">{d.conditions}</span>
                <span>
                  {Math.round(d.min_c)}–{Math.round(d.max_c)}°
                </span>
              </li>
            ))}
          </ul>
        </>
      ) : (
        <p className="text-amber">{data.weather_error}</p>
      )}
      <h3 className="hud-title mt-4">Today</h3>
      {data.events.length ? (
        <ul className="m-0 flex list-none flex-col gap-1.5 p-0 text-sm">
          {data.events.map((e, i) => (
            <li key={i}>
              <span className="font-mono text-xs text-cyan">{e.start.slice(-5)}</span> {e.subject}
              {e.location ? <span className="block text-xs text-muted">{e.location}</span> : null}
            </li>
          ))}
        </ul>
      ) : (
        <p className="hint">No events — or no calendar connected (Connections).</p>
      )}
      <p className="hint mt-3 text-xs">{data.source}</p>
    </>
  );
}

function MapsOff({ settings, onEnabled }: { settings: SettingsView | null; onEnabled: () => void }) {
  const { toast } = useApp();
  const [city, setCity] = useState(settings?.profile.location || "");
  const [busy, setBusy] = useState(false);
  const enable = async () => {
    setBusy(true);
    try {
      await put("/v1/settings", {
        profile: { location: city.trim() },
        features: { maps: { enabled: true } },
      });
      onEnabled();
    } catch (err) {
      toast(errorText(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="m-auto max-w-lg p-6">
      <div className="hud-panel p-6">
        <h1 className="hud-title text-lg">Live maps</h1>
        <p className="mt-2">
          Three live panels: aircraft flying near you, which programs on this PC talk to the internet (and where), and
          your weather with today's agenda.
        </p>
        <p className="hint">
          Your city's coordinates are sent to the OpenSky Network (flights) and Open-Meteo (weather). Connection
          locations are looked up on this PC. Map tiles come from OpenFreeMap.
        </p>
        <label className="field">
          Your city
          <input type="text" maxLength={100} placeholder="e.g. Bengaluru, India" value={city} onChange={(e) => setCity(e.target.value)} />
        </label>
        <button className="btn btn-primary" type="button" disabled={busy || !city.trim()} onClick={() => void enable()}>
          Turn on live maps
        </button>
      </div>
    </div>
  );
}

export function MapsView() {
  const { state, toast } = useApp();
  const [settings, setSettings] = useState<SettingsView | null>(null);
  const [panel, setPanel] = useState<Panel>("sky");
  const [sky, setSky] = useState<SkyView | null>(null);
  const [net, setNet] = useState<NetworkView | null>(null);
  const [sit, setSit] = useState<SituationView | null>(null);
  const [search, setSearch] = useState("");
  const container = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MLMap | null>(null);
  const framed = useRef(false);
  const [ready, setReady] = useState(false);

  const loadSettings = useCallback(() => {
    api<SettingsView>("/v1/settings").then(setSettings).catch((err) => toast(errorText(err)));
  }, [toast]);
  useEffect(loadSettings, [loadSettings]);

  const maps = settings?.features.maps;
  const enabledPanels = PANELS.filter((p) => maps?.[p.id]);
  const on = Boolean(maps?.enabled && settings?.profile.latitude !== null && enabledPanels.length);

  // Create the map once the feature is on.
  useEffect(() => {
    if (!on || !container.current || mapRef.current) return;
    const p = settings!.profile;
    const map = new MapLibre({
      container: container.current,
      style: HUD_STYLE,
      center: [p.longitude ?? 0, p.latitude ?? 20],
      zoom: zoomForRadius(maps!.sky_radius_km),
      attributionControl: { compact: true },
      maxPitch: 60,
    });
    map.addControl(new NavigationControl({ visualizePitch: true }), "bottom-right");
    map.on("load", () => {
      addLayers(map);
      setReady(true);
    });
    map.on("click", "sky-planes", (e) => {
      const f = e.features?.[0];
      if (!f) return;
      const a = f.properties as Record<string, any>;
      new Popup({ closeButton: false })
        .setLngLat(e.lngLat)
        .setDOMContent(
          popup([
            ["Flight", String(a.callsign)],
            ["From", String(a.country)],
            ["Altitude", a.on_ground ? "on the ground" : fmt(a.altitude_m, "m")],
            ["Speed", fmt(a.speed_kmh, "km/h")],
            ["Heading", fmt(a.heading, "°")],
            ["Squawk", `${a.squawk || "—"}${a.alert ? ` (${a.alert})` : ""}`],
            ["Distance", `${a.distance_km} km`],
          ]),
        )
        .addTo(map);
    });
    map.on("click", "net-countries", (e) => {
      const f = e.features?.[0];
      if (!f) return;
      const a = f.properties as Record<string, any>;
      new Popup({ closeButton: false })
        .setLngLat(e.lngLat)
        .setDOMContent(popup([["Country", String(a.name)], ["Connections", String(a.count)], ["Programs", String(a.programs)]]))
        .addTo(map);
    });
    for (const layer of ["sky-planes", "net-countries"]) {
      map.on("mouseenter", layer, () => (map.getCanvas().style.cursor = "pointer"));
      map.on("mouseleave", layer, () => (map.getCanvas().style.cursor = ""));
    }
    mapRef.current = map;
    return () => {
      map.remove();
      mapRef.current = null;
      setReady(false);
    };
  }, [on]);

  // Keep the active panel valid as settings change.
  useEffect(() => {
    if (enabledPanels.length && !enabledPanels.some((p) => p.id === panel)) setPanel(enabledPanels[0].id);
  }, [settings]);

  // Home marker and panel visibility.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready || !settings) return;
    const p = settings.profile;
    if (p.latitude !== null && p.longitude !== null) {
      setData(map, "home", {
        type: "FeatureCollection",
        features: [{ type: "Feature", properties: {}, geometry: { type: "Point", coordinates: [p.longitude, p.latitude] } }],
      });
    }
    showPanel(map, panel);
  }, [ready, panel, settings]);

  // Poll the active panel while it's on screen.
  useEffect(() => {
    if (!on || !state.connected) return;
    let alive = true;
    const load = async () => {
      if (document.hidden) return;
      try {
        if (panel === "sky") setSky(await api<SkyView>("/v1/maps/sky"));
        if (panel === "network") setNet(await api<NetworkView>("/v1/maps/network"));
        if (panel === "situation") setSit(await api<SituationView>("/v1/maps/situation"));
      } catch (err) {
        if (alive) toast(errorText(err));
      }
    };
    void load();
    const timer = window.setInterval(load, POLL_MS[panel]);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [on, panel, state.connected, toast]);

  // Push data into the map.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready || !sky) return;
    const planes = sky.aircraft || [];
    setData(map, "sky", {
      type: "FeatureCollection",
      features: planes.map((a) => ({
        type: "Feature",
        properties: { ...a },
        geometry: { type: "Point", coordinates: [a.lon, a.lat] },
      })),
    });
    if (sky.home) {
      const rings = [sky.radius_km, sky.radius_km / 2].map((r) => ({
        type: "Feature" as const,
        properties: {},
        geometry: { type: "LineString" as const, coordinates: circle(sky.home!.lon, sky.home!.lat, r) },
      }));
      setData(map, "sky-rings", { type: "FeatureCollection", features: rings });
    }
  }, [sky, ready]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready || !net) return;
    const byCountry = new globalThis.Map<string, { name: string; lat: number; lon: number; count: number; flagged: boolean; programs: Set<string> }>();
    for (const p of net.peers) {
      if (!p.country) continue;
      const c = byCountry.get(p.country.code) || { ...p.country, count: 0, flagged: false, programs: new Set<string>() };
      c.count += p.connections;
      c.flagged ||= p.flags.length > 0;
      c.programs.add(p.process || "unknown");
      byCountry.set(p.country.code, c);
    }
    const countries = [...byCountry.values()];
    setData(map, "net-countries", {
      type: "FeatureCollection",
      features: countries.map((c) => ({
        type: "Feature",
        properties: { name: c.name, count: c.count, flagged: c.flagged, programs: [...c.programs].slice(0, 6).join(", ") },
        geometry: { type: "Point", coordinates: [c.lon, c.lat] },
      })),
    });
    const home = net.home;
    setData(map, "net-arcs", {
      type: "FeatureCollection",
      features: home
        ? countries.map((c) => ({
            type: "Feature",
            properties: { count: c.count, flagged: c.flagged },
            geometry: { type: "LineString", coordinates: arc([home.lon, home.lat], [c.lon, c.lat]) },
          }))
        : [],
    });
  }, [net, ready]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready || !sit) return;
    // Frame home and today's places together the first time they're shown.
    const spots = sit.events.filter((e) => e.lat !== undefined && e.lon !== undefined);
    if (spots.length && sit.home && panel === "situation" && !framed.current) {
      framed.current = true;
      const lons = [sit.home.lon, ...spots.map((e) => e.lon!)];
      const lats = [sit.home.lat, ...spots.map((e) => e.lat!)];
      map.fitBounds(
        [
          [Math.min(...lons), Math.min(...lats)],
          [Math.max(...lons), Math.max(...lats)],
        ],
        { padding: { top: 120, bottom: 80, left: 80, right: 360 }, maxZoom: 13, duration: 800 },
      );
    }
    setData(map, "sit-events", {
      type: "FeatureCollection",
      features: sit.events
        .filter((e) => e.lat !== undefined && e.lon !== undefined)
        .map((e) => ({
          type: "Feature",
          properties: { subject: `${e.start.slice(-5)} ${e.subject}` },
          geometry: { type: "Point", coordinates: [e.lon!, e.lat!] },
        })),
    });
  }, [sit, ready, panel]);

  // Camera per panel, and focus requests from the agent ("show me Mumbai").
  const fly = useCallback(
    (p: Panel, lat?: number, lon?: number) => {
      const map = mapRef.current;
      if (!map || !settings) return;
      const home = settings.profile;
      const focused = lat !== undefined && lon !== undefined;
      if (p === "network" && !focused) {
        // The whole world: connections can go anywhere.
        map.flyTo({ center: [10, 25], zoom: Math.max(0.8, Math.log2(map.getContainer().clientWidth / 512) - 0.05), pitch: 0, essential: true });
        return;
      }
      const center: [number, number] = [lon ?? home.longitude ?? 0, lat ?? home.latitude ?? 20];
      const zoom = p === "network" ? 4 : p === "sky" ? zoomForRadius(settings.features.maps.sky_radius_km) : 9;
      map.flyTo({ center, zoom, pitch: p === "sky" ? 35 : 0, essential: true });
    },
    [settings],
  );
  const choose = (p: Panel) => {
    setPanel(p);
    fly(p);
  };
  useEffect(() => {
    const f = state.mapFocus;
    if (!f || !ready) return;
    setPanel(f.view);
    fly(f.view, f.lat, f.lon);
  }, [state.mapFocus, ready, fly]);

  const goTo = async () => {
    const q = search.trim();
    if (!q) return;
    try {
      const place = await api<{ name: string; lat: number; lon: number }>(`/v1/maps/locate?q=${encodeURIComponent(q)}`);
      mapRef.current?.flyTo({ center: [place.lon, place.lat], zoom: 9, essential: true });
    } catch (err) {
      toast(errorText(err));
    }
  };

  if (!settings) return <div className="m-auto text-muted">Loading…</div>;
  if (!on) return <MapsOff settings={settings} onEnabled={loadSettings} />;

  return (
    <div className="relative min-h-0 flex-1">
      <div className="absolute inset-0">
        {/* MapLibre's CSS makes its container position:relative, so it needs a sized box. */}
        <div ref={container} className="h-full w-full" aria-label="Map" />
      </div>
      <div className="pointer-events-none absolute inset-x-3 top-3 flex flex-wrap items-start gap-2">
        <div className="hud-panel pointer-events-auto flex gap-1 p-1" role="tablist">
          {enabledPanels.map((p) => (
            <button
              key={p.id}
              role="tab"
              aria-selected={panel === p.id}
              className={`btn btn-sm ${panel === p.id ? "btn-primary" : "btn-ghost"}`}
              type="button"
              onClick={() => choose(p.id)}
            >
              {p.label}
            </button>
          ))}
        </div>
        <form
          className="hud-panel pointer-events-auto flex gap-1 p-1"
          onSubmit={(e) => {
            e.preventDefault();
            void goTo();
          }}
        >
          <input
            type="text"
            className="w-44 py-0.5"
            placeholder="Fly to a place…"
            aria-label="Fly to a place"
            maxLength={100}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <button className="btn btn-sm" type="submit">
            Go
          </button>
        </form>
      </div>
      <aside className="hud-panel absolute top-16 right-3 max-h-[calc(100%-7rem)] w-72 overflow-auto p-4" aria-label="Panel details">
        {panel === "sky" && <SkyInfo data={sky} />}
        {panel === "network" && <NetworkInfo data={net} />}
        {panel === "situation" && <SituationInfo data={sit} />}
      </aside>
    </div>
  );
}
