"use client";

import type { GeoJSONSource } from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { Globe2, Maximize2 } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { createMap, type LoadedMap, primarySource, type SourceColors, WORLD_VIEW } from "../../lib/map";
import { useResolvedTheme } from "../../lib/theme";
import { motionAllowed } from "../../lib/zoom";
import { Button } from "../ui/button";
import { Select } from "../ui/field";
import { Faint, providerLabel } from "../ui/primitives";
import styles from "./map.module.css";

export type PlacePoint = {
  label: string; latitude: number; longitude: number; count: number; first_seen?: string | null; last_seen?: string | null;
  sources?: string[]; source_kind?: string; events?: { at: string; text: string; url?: string; source: string }[];
};

function collection(points: PlacePoint[], colors: SourceColors): GeoJSON.FeatureCollection {
  return {
    type: "FeatureCollection",
    features: points.map((point, index) => ({
      type: "Feature", geometry: { type: "Point", coordinates: [point.longitude, point.latitude] },
      properties: { i: index, label: point.label, count: point.count, color: colors[primarySource(point.sources)] },
    })),
  };
}

/** Places on a map, filterable by year and source; selecting one lists what happened there. */
export function PlacesMap({ points }: { points: PlacePoint[] }) {
  const theme = useResolvedTheme();
  const container = useRef<HTMLDivElement>(null);
  const loaded = useRef<LoadedMap | null>(null);
  const [ready, setReady] = useState(false);
  const [year, setYear] = useState("");
  const [source, setSource] = useState("");
  const [selected, setSelected] = useState<PlacePoint | null>(null);
  const years = useMemo(() => [...new Set(points.flatMap((point) => (point.events ?? []).map((event) => event.at?.slice(0, 4))).filter(Boolean))].sort().reverse(), [points]);
  const sources = useMemo(() => [...new Set(points.flatMap((point) => point.sources ?? []))], [points]);
  const visible = useMemo(() => points.filter((point) => (!source || point.sources?.includes(source))
    && (!year || point.events?.some((event) => event.at?.startsWith(year)))), [points, source, year]);
  const latest = useRef(visible);
  latest.current = visible;
  const first = useRef(true);

  useEffect(() => {
    let cancelled = false;
    void createMap(container.current!).then((result) => {
      if (cancelled) {
        result.destroy();
        return;
      }
      loaded.current = result;
      const { map, maplibregl, colors } = result;
      map.addSource("places", { type: "geojson", data: collection([], colors) });
      map.addLayer({
        id: "places", type: "circle", source: "places",
        paint: {
          "circle-color": ["get", "color"], "circle-radius": ["min", 15, ["+", 6, ["sqrt", ["get", "count"]]]],
          "circle-stroke-color": colors.surface, "circle-stroke-width": 2, "circle-opacity": 0.9,
        },
      });
      const popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 10, className: styles.popup });
      map.on("click", "places", (event) => setSelected(latest.current[event.features![0].properties.i as number]));
      map.on("mouseenter", "places", () => { map.getCanvas().style.cursor = "pointer"; });
      map.on("mouseleave", "places", () => {
        map.getCanvas().style.cursor = "";
        popup.remove();
      });
      map.on("mousemove", "places", (event) => {
        const feature = event.features![0];
        popup.setLngLat((feature.geometry as GeoJSON.Point).coordinates as [number, number]).setText(feature.properties.label as string).addTo(map);
      });
      first.current = true;
      setReady(true);
    });
    return () => {
      cancelled = true;
      loaded.current?.destroy();
      loaded.current = null;
      setReady(false);
    };
  }, [theme]);

  function fit(maxZoom: number, instant = false) {
    const map = loaded.current?.map;
    if (!map || !latest.current.length) return;
    const lons = latest.current.map((point) => point.longitude);
    const lats = latest.current.map((point) => point.latitude);
    map.fitBounds([[Math.min(...lons), Math.min(...lats)], [Math.max(...lons), Math.max(...lats)]],
      { padding: 42, maxZoom, duration: instant || !motionAllowed() ? 0 : 600 });
  }
  useEffect(() => {
    if (!ready) return;
    (loaded.current!.map.getSource("places") as GeoJSONSource).setData(collection(visible, loaded.current!.colors));
    fit(visible.length === 1 ? 10 : 12, first.current);
    first.current = false;
  }, [ready, visible]);

  function show(point: PlacePoint) {
    setSelected(point);
    loaded.current?.map.flyTo({ center: [point.longitude, point.latitude], zoom: 10, speed: 1.6, animate: motionAllowed() });
  }
  const events = (selected?.events ?? []).filter((event) => (!year || event.at.startsWith(year)) && (!source || event.source === source)).slice(0, 12);

  return <div className={styles.places}>
    <div className={styles.toolbar}>
      {years.length > 1 && <Select aria-label="年份" value={year} onChange={(event) => setYear(event.target.value)}>
        <option value="">全部年份</option>{years.map((value) => <option key={value}>{value}</option>)}
      </Select>}
      {sources.length > 1 && <Select aria-label="来源" value={source} onChange={(event) => setSource(event.target.value)}>
        <option value="">全部平台</option>{sources.map((value) => <option value={value} key={value}>{providerLabel(value)}</option>)}
      </Select>}
      <span className={styles.spacer} />
      <Button size="sm" variant="ghost" onClick={() => loaded.current?.map.flyTo({ ...WORLD_VIEW, zoom: 0, speed: 1.8, animate: motionAllowed() })}><Globe2 size={14} />全球</Button>
      <Button size="sm" variant="ghost" onClick={() => fit(10)}><Maximize2 size={14} />全部地点</Button>
    </div>
    <div className={styles.canvas} ref={container} role="region" aria-label="足迹地图，可双指滑动或捏合缩放，拖动平移" data-map-ready={ready ? "" : undefined} />
    {ready && loaded.current?.offline && <Faint>详细底图暂时无法连接，正在显示本地世界轮廓。</Faint>}
    <div className={styles.placeChips}>{visible.slice(0, 12).map((point) => <button type="button" key={`${point.label}-${point.latitude}-${point.longitude}`}
      aria-pressed={selected === point} onClick={() => show(point)}>{point.label}<small>{point.count ? `${point.count} 次` : "资料地址"}</small></button>)}</div>
    {selected && <div className={styles.detail} aria-live="polite">
      <header><strong>{selected.label}</strong><small>{selected.latitude.toFixed(4)}, {selected.longitude.toFixed(4)}</small>
        <a href={`https://www.openstreetmap.org/?mlat=${selected.latitude}&mlon=${selected.longitude}#map=14/${selected.latitude}/${selected.longitude}`} target="_blank" rel="noreferrer">OpenStreetMap</a>
      </header>
      {events.length > 0 && <ol>{events.map((event, index) => <li key={index}>
        <time>{event.at.slice(0, 10)}</time>{event.url ? <a href={event.url}>{event.text || "查看动态"}</a> : <span>{event.text || "已收录足迹"}</span>}
      </li>)}</ol>}
    </div>}
  </div>;
}
