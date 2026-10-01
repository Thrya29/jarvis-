// A dark "HUD" basemap on OpenFreeMap's vector tiles (OpenMapTiles schema), plus
// geometry helpers for the panels. Only tiles and label glyphs come from the network.

import type { StyleSpecification } from "maplibre-gl";

export const TILE_HOST = "https://tiles.openfreemap.org";
const NAME = ["coalesce", ["get", "name:en"], ["get", "name_en"], ["get", "name"]];
const FONT = ["Noto Sans Regular"];

export const HUD_STYLE: StyleSpecification = {
  version: 8,
  glyphs: `${TILE_HOST}/fonts/{fontstack}/{range}.pbf`,
  sources: {
    omt: { type: "vector", url: `${TILE_HOST}/planet` },
  },
  layers: [
    { id: "bg", type: "background", paint: { "background-color": "#020a12" } },
    { id: "water", type: "fill", source: "omt", "source-layer": "water", paint: { "fill-color": "#04182a" } },
    {
      id: "park",
      type: "fill",
      source: "omt",
      "source-layer": "park",
      paint: { "fill-color": "#03141c", "fill-opacity": 0.7 },
    },
    {
      id: "waterway",
      type: "line",
      source: "omt",
      "source-layer": "waterway",
      minzoom: 8,
      paint: { "line-color": "#06263c", "line-width": 1 },
    },
    {
      id: "building",
      type: "fill",
      source: "omt",
      "source-layer": "building",
      minzoom: 13,
      paint: { "fill-color": "#0a2232", "fill-outline-color": "#0f3a50" },
    },
    {
      id: "road-minor",
      type: "line",
      source: "omt",
      "source-layer": "transportation",
      minzoom: 11,
      filter: ["in", ["get", "class"], ["literal", ["secondary", "tertiary", "minor", "street"]]],
      paint: { "line-color": "#08273a", "line-width": ["interpolate", ["linear"], ["zoom"], 11, 0.5, 16, 3] },
    },
    {
      id: "road-major",
      type: "line",
      source: "omt",
      "source-layer": "transportation",
      minzoom: 5,
      filter: ["in", ["get", "class"], ["literal", ["motorway", "trunk", "primary"]]],
      paint: {
        "line-color": "#0c4258",
        "line-width": ["interpolate", ["linear"], ["zoom"], 5, 0.4, 10, 1.4, 16, 5],
      },
    },
    {
      id: "runway",
      type: "line",
      source: "omt",
      "source-layer": "aeroway",
      minzoom: 8,
      filter: ["==", ["get", "class"], "runway"],
      paint: { "line-color": "#1f7a96", "line-width": ["interpolate", ["linear"], ["zoom"], 8, 1, 14, 10] },
    },
    {
      id: "boundary-state",
      type: "line",
      source: "omt",
      "source-layer": "boundary",
      minzoom: 3,
      filter: ["all", ["==", ["get", "admin_level"], 4], ["!=", ["get", "maritime"], 1]],
      paint: { "line-color": "#0d3a4c", "line-width": 0.8, "line-dasharray": [3, 2] },
    },
    {
      id: "boundary-country",
      type: "line",
      source: "omt",
      "source-layer": "boundary",
      filter: ["all", ["==", ["get", "admin_level"], 2], ["!=", ["get", "maritime"], 1]],
      paint: { "line-color": "#1d6f8a", "line-width": ["interpolate", ["linear"], ["zoom"], 1, 0.6, 8, 1.6] },
    },
    {
      id: "place-country",
      type: "symbol",
      source: "omt",
      "source-layer": "place",
      maxzoom: 7,
      filter: ["==", ["get", "class"], "country"],
      layout: {
        "text-field": NAME as never,
        "text-font": FONT,
        "text-size": ["interpolate", ["linear"], ["zoom"], 1, 9, 6, 13],
        "text-transform": "uppercase",
        "text-letter-spacing": 0.2,
      },
      paint: { "text-color": "#3f9fbd", "text-halo-color": "#020a12", "text-halo-width": 1.2 },
    },
    {
      id: "place-city",
      type: "symbol",
      source: "omt",
      "source-layer": "place",
      minzoom: 4,
      filter: ["in", ["get", "class"], ["literal", ["city", "town"]]],
      layout: {
        "text-field": NAME as never,
        "text-font": FONT,
        "text-size": ["interpolate", ["linear"], ["zoom"], 4, 10, 10, 14],
      },
      paint: { "text-color": "#8fd8ec", "text-halo-color": "#020a12", "text-halo-width": 1.2 },
    },
  ],
};

type Position = [number, number];

/** A geodesic circle (polygon ring) of radius_km around a point. */
export function circle(lon: number, lat: number, radiusKm: number, steps = 96): Position[] {
  const R = 6371;
  const d = radiusKm / R;
  const φ1 = (lat * Math.PI) / 180;
  const λ1 = (lon * Math.PI) / 180;
  const ring: Position[] = [];
  for (let i = 0; i <= steps; i++) {
    const θ = (2 * Math.PI * i) / steps;
    const φ2 = Math.asin(Math.sin(φ1) * Math.cos(d) + Math.cos(φ1) * Math.sin(d) * Math.cos(θ));
    const λ2 = λ1 + Math.atan2(Math.sin(θ) * Math.sin(d) * Math.cos(φ1), Math.cos(d) - Math.sin(φ1) * Math.sin(φ2));
    ring.push([(λ2 * 180) / Math.PI, (φ2 * 180) / Math.PI]);
  }
  return ring;
}

/**
 * A gently curved line between two points, drawn the short way round in longitude.
 * (A true great circle shoots off the top of a flat map for long east-west routes.)
 */
export function arc(a: Position, b: Position, steps = 48): Position[] {
  let [x2, y2] = b;
  const [x1, y1] = a;
  while (x2 - x1 > 180) x2 -= 360;
  while (x2 - x1 < -180) x2 += 360;
  const dx = x2 - x1;
  const dy = y2 - y1;
  const len = Math.hypot(dx, dy);
  if (len < 1e-6) return [a, b];
  // Control point: the midpoint pushed sideways (towards the pole side) by 18% of the length.
  const side = dx >= 0 ? 1 : -1;
  const cx = (x1 + x2) / 2 - (dy / len) * len * 0.18 * side;
  const cy = Math.max(-80, Math.min(80, (y1 + y2) / 2 + (dx / len) * len * 0.18 * side));
  const pts: Position[] = [];
  for (let i = 0; i <= steps; i++) {
    const t = i / steps;
    const u = 1 - t;
    pts.push([u * u * x1 + 2 * u * t * cx + t * t * x2, u * u * y1 + 2 * u * t * cy + t * t * y2]);
  }
  return pts;
}

/** A plane silhouette pointing north, as an SDF-able image (tinted per aircraft). */
export function planeImage(size = 48): { width: number; height: number; data: Uint8Array } {
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = size;
  const c = canvas.getContext("2d")!;
  const s = size / 48;
  c.fillStyle = "#fff";
  c.beginPath();
  // fuselage
  c.moveTo(24 * s, 2 * s);
  c.lineTo(27 * s, 10 * s);
  c.lineTo(27 * s, 19 * s);
  // right wing
  c.lineTo(45 * s, 28 * s);
  c.lineTo(45 * s, 32 * s);
  c.lineTo(27 * s, 27 * s);
  c.lineTo(26.5 * s, 38 * s);
  // right tail
  c.lineTo(33 * s, 43 * s);
  c.lineTo(33 * s, 46 * s);
  c.lineTo(24 * s, 43.5 * s);
  // left tail
  c.lineTo(15 * s, 46 * s);
  c.lineTo(15 * s, 43 * s);
  c.lineTo(21.5 * s, 38 * s);
  c.lineTo(21 * s, 27 * s);
  // left wing
  c.lineTo(3 * s, 32 * s);
  c.lineTo(3 * s, 28 * s);
  c.lineTo(21 * s, 19 * s);
  c.lineTo(21 * s, 10 * s);
  c.closePath();
  c.fill();
  const img = c.getImageData(0, 0, size, size);
  return { width: size, height: size, data: new Uint8Array(img.data.buffer) };
}

export function zoomForRadius(km: number): number {
  return Math.max(3, Math.min(11, Math.log2(40000 / km) - 1.2));
}
