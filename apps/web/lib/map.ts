import type { LngLatLike, Map as MapLibreMap, StyleSpecification } from "maplibre-gl";

import { cssVar, resolvedTheme } from "./theme";

const STYLE_URL = { light: "https://tiles.openfreemap.org/styles/positron", dark: "https://tiles.openfreemap.org/styles/dark" };
const GLYPHS = "https://tiles.openfreemap.org/fonts/{fontstack}/{range}.pbf";
export const LABEL_FONT = ["Noto Sans Bold"];
export const WORLD_VIEW = { center: [10, 22] as LngLatLike, zoom: 1 };

export type SourceColors = Record<"wechat" | "instagram" | "linkedin", string> & { surface: string; ink: string; accent: string };

/** Source colours from the current palette; MapLibre paint needs literal colours, not CSS variables. */
export function sourceColors(): SourceColors {
  return {
    wechat: cssVar("--source-wechat"), instagram: cssVar("--source-instagram"), linkedin: cssVar("--source-linkedin"),
    surface: cssVar("--color-surface"), ink: cssVar("--color-text"), accent: cssVar("--color-accent"),
  };
}

/** The source a point is drawn in, by precedence: LinkedIn, then Instagram, then WeChat. */
export function primarySource(sources: string[] = []): "linkedin" | "instagram" | "wechat" {
  return sources.includes("linkedin") ? "linkedin" : sources.includes("instagram") ? "instagram" : "wechat";
}

/** Local world outlines, shown when the vector basemap cannot be reached. */
function offlineStyle(): StyleSpecification {
  return {
    version: 8, glyphs: GLYPHS,
    sources: { world: { type: "geojson", data: "/vendor/world.geojson" } },
    layers: [
      { id: "background", type: "background", paint: { "background-color": cssVar("--map-water") } },
      { id: "land", type: "fill", source: "world", paint: { "fill-color": cssVar("--map-land") } },
      { id: "borders", type: "line", source: "world", paint: { "line-color": cssVar("--map-border"), "line-width": 0.6 } },
    ],
  };
}

async function basemap(): Promise<{ style: StyleSpecification | string; offline: boolean }> {
  const url = STYLE_URL[resolvedTheme()];
  const response = await fetch(url, { signal: AbortSignal.timeout(6000) }).catch(() => null);
  return response?.ok ? { style: url, offline: false } : { style: offlineStyle(), offline: true };
}

/**
 * A GPU-rendered map with continuous zoom, trackpad/touch pinch and inertia; resolves once the style has loaded.
 * Each map lives in its own child element, so a map whose effect was cancelled while loading can be destroyed
 * without touching a newer map in the same container. Callers recreate the map when the theme changes.
 */
export async function createMap(container: HTMLElement, view = WORLD_VIEW) {
  const host = document.createElement("div");
  host.style.cssText = "position:absolute;inset:0";
  container.style.position = "relative";
  container.append(host);
  const [{ default: maplibregl }, { style, offline }] = await Promise.all([import("maplibre-gl"), basemap()]);
  const map = new maplibregl.Map({
    container: host, style, ...view, minZoom: 0, maxZoom: 18,
    attributionControl: { compact: true }, dragRotate: false, pitchWithRotate: false, fadeDuration: 150,
  });
  map.touchZoomRotate.disableRotation();
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
  const destroy = () => { map.remove(); host.remove(); };
  await new Promise<void>((resolve) => map.once("load", () => resolve()));
  // Compact attribution starts expanded; on a narrow map that covers the points.
  host.querySelector(".maplibregl-ctrl-attrib")?.classList.remove("maplibregl-compact-show");
  container.dataset.mapReady = "";
  return { map, maplibregl, offline, destroy, colors: sourceColors() };
}

export type LoadedMap = Awaited<ReturnType<typeof createMap>>;
export type { MapLibreMap };
