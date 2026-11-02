"use client";

import type { GeoJSONSource, GeoJSONSourceSpecification } from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { Globe2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { type AtlasPoint, area, collection, trail } from "../../lib/footprint";
import { createMap, LABEL_FONT, type LoadedMap, WORLD_VIEW } from "../../lib/map";
import { motionAllowed } from "../../lib/zoom";
import mapStyles from "../map/map.module.css";
import { Button } from "../ui/button";
import { Faint } from "../ui/primitives";
import styles from "./footprint.module.css";

const ATLAS_LAYERS = ["atlas-clusters", "atlas-cluster-count", "atlas-points"];
const TRAIL_LAYERS = ["trail-past", "trail-now"];

/**
 * Everyone's places, clustered. Clicking a cluster both lists what it holds and zooms towards it. During playback
 * the clusters give way to a trail: the current month's places lit, earlier ones fading.
 */
export function FootprintWorld({ points, selected, onSelect, playback }: {
  points: AtlasPoint[]; selected: AtlasPoint | null; onSelect: (point: AtlasPoint) => void;
  playback: { indexes: number[][]; month: number } | null;
}) {
  const container = useRef<HTMLDivElement>(null);
  const loaded = useRef<LoadedMap | null>(null);
  const [ready, setReady] = useState(false);
  const latest = useRef({ points, onSelect });
  latest.current = { points, onSelect };

  useEffect(() => {
    let cancelled = false;
    void createMap(container.current!).then((result) => {
      if (cancelled) {
        result.destroy();
        return;
      }
      loaded.current = result;
      const { map, maplibregl, colors } = result;
      const sourceColor = ["match", ["get", "source"], "linkedin", colors.linkedin, "instagram", colors.instagram, colors.wechat];
      const count = (source: string) => ["+", ["case", ["==", ["get", "source"], source], 1, 0]];
      map.addSource("atlas", {
        type: "geojson", data: collection([]), cluster: true, clusterRadius: 48, clusterMaxZoom: 15,
        clusterProperties: { linkedin: count("linkedin"), instagram: count("instagram"), wechat: count("wechat") },
      } as GeoJSONSourceSpecification);
      map.addLayer({
        id: "atlas-clusters", type: "circle", source: "atlas", filter: ["has", "point_count"], paint: {
          "circle-color": ["case", ["all", [">=", ["get", "linkedin"], ["get", "instagram"]], [">=", ["get", "linkedin"], ["get", "wechat"]]], colors.linkedin,
            [">=", ["get", "instagram"], ["get", "wechat"]], colors.instagram, colors.wechat],
          "circle-opacity": 0.85, "circle-radius": ["interpolate", ["linear"], ["get", "point_count"], 2, 13, 50, 18, 500, 23],
          "circle-stroke-color": colors.surface, "circle-stroke-width": 2,
        },
      });
      map.addLayer({
        id: "atlas-cluster-count", type: "symbol", source: "atlas", filter: ["has", "point_count"],
        layout: { "text-field": ["get", "point_count_abbreviated"], "text-font": LABEL_FONT, "text-size": 12, "text-allow-overlap": true },
        paint: { "text-color": "#fff" },
      });
      map.addLayer({
        id: "atlas-points", type: "circle", source: "atlas", filter: ["!", ["has", "point_count"]],
        paint: { "circle-color": sourceColor as never, "circle-radius": 7, "circle-stroke-color": colors.surface, "circle-stroke-width": 2 },
      });
      map.addSource("trail", { type: "geojson", data: collection([]) });
      map.addLayer({
        id: "trail-past", type: "circle", source: "trail", filter: ["!", ["get", "now"]], layout: { visibility: "none" },
        paint: { "circle-color": sourceColor as never, "circle-radius": 4, "circle-opacity": ["get", "fade"] },
      });
      map.addLayer({
        id: "trail-now", type: "circle", source: "trail", filter: ["get", "now"], layout: { visibility: "none" },
        paint: { "circle-color": sourceColor as never, "circle-radius": 8, "circle-stroke-color": colors.surface, "circle-stroke-width": 2 },
      });

      const source = () => map.getSource("atlas") as GeoJSONSource;
      map.on("click", "atlas-clusters", async (event) => {
        const feature = event.features![0];
        const id = feature.properties.cluster_id as number;
        const center = (feature.geometry as GeoJSON.Point).coordinates as [number, number];
        const [zoom, leaves] = await Promise.all([source().getClusterExpansionZoom(id), source().getClusterLeaves(id, Infinity, 0)]);
        latest.current.onSelect(area(leaves.map((leaf) => latest.current.points[leaf.properties!.i as number])));
        // Points that overlap at street level only list; there is nothing further to zoom into.
        if (zoom <= 16 && map.getZoom() < 16) map.easeTo({ center, zoom: zoom + 0.25, duration: motionAllowed() ? 450 : 0 });
      });
      for (const layer of ["atlas-points", "trail-now", "trail-past"]) {
        map.on("click", layer, (event) => latest.current.onSelect(latest.current.points[event.features![0].properties.i as number]));
      }
      const popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 10, className: mapStyles.popup });
      for (const layer of ["atlas-clusters", "atlas-points", "trail-now", "trail-past"]) {
        map.on("mouseenter", layer, () => { map.getCanvas().style.cursor = "pointer"; });
        map.on("mouseleave", layer, () => {
          map.getCanvas().style.cursor = "";
          popup.remove();
        });
      }
      for (const layer of ["atlas-points", "trail-now", "trail-past"]) {
        map.on("mousemove", layer, (event) => {
          const feature = event.features![0];
          popup.setLngLat((feature.geometry as GeoJSON.Point).coordinates as [number, number]).setText(feature.properties.label as string).addTo(map);
        });
      }
      setReady(true);
    });
    return () => {
      cancelled = true;
      loaded.current?.destroy();
      loaded.current = null;
      setReady(false);
    };
  }, []);

  useEffect(() => {
    if (ready) (loaded.current!.map.getSource("atlas") as GeoJSONSource).setData(collection(points));
  }, [points, ready]);

  const playing = playback != null;
  useEffect(() => {
    if (!ready) return;
    const map = loaded.current!.map;
    for (const layer of ATLAS_LAYERS) map.setLayoutProperty(layer, "visibility", playing ? "none" : "visible");
    for (const layer of TRAIL_LAYERS) map.setLayoutProperty(layer, "visibility", playing ? "visible" : "none");
  }, [playing, ready]);

  useEffect(() => {
    if (ready && playback) (loaded.current!.map.getSource("trail") as GeoJSONSource).setData(trail(points, playback.indexes, playback.month));
  }, [points, playback, ready]);

  useEffect(() => {
    if (ready && selected && !selected.places) {
      const map = loaded.current!.map;
      map.flyTo({ center: [selected.longitude, selected.latitude], zoom: Math.max(10, map.getZoom()), speed: 1.6, animate: motionAllowed() });
    }
  }, [selected, ready]);

  return <div className={styles.mapFrame}>
    <div className={`${mapStyles.canvas} ${styles.map}`} ref={container} role="region"
      aria-label="所有人的足迹地图，可双指滑动或捏合缩放，拖动平移，点击聚合地点放大" />
    <Button size="sm" variant="secondary" className={styles.world}
      onClick={() => loaded.current?.map.flyTo({ ...WORLD_VIEW, speed: 1.8, animate: motionAllowed() })}><Globe2 size={14} />全球</Button>
    {ready && loaded.current?.offline && <Faint>详细底图暂时无法连接，正在显示本地世界轮廓。</Faint>}
  </div>;
}
