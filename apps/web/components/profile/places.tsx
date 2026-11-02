"use client";

import { ExternalLink, MapPin } from "lucide-react";

import { useProfileTab } from "../../lib/queries";
import { PlacesMap, type PlacePoint } from "../map/places-map";
import { Faint, Panel, Skeleton, providerLabel } from "../ui/primitives";
import { formatDate } from "./fields";
import styles from "./profile.module.css";

type LocationSummary = { place_count: number; total_posts_with_location: number; points: (PlacePoint & { map_url?: string })[] };

const LISTED = 8;

/** Footprint on a small map with the most recent places listed. Absent when there is nothing to place. */
export function LocationSummary({ profileId }: { profileId: string }) {
  const locations = useProfileTab<LocationSummary>(profileId, "locations");
  const summary = locations.data;
  if (locations.isError || (summary && !summary.points.length)) return null;
  return <Panel className={styles.block} label="足迹与地点">
    <header className={styles.blockHeader}>
      <h2><MapPin size={15} aria-hidden="true" />足迹</h2>
      {summary && <small>{summary.place_count} 个地点 · {summary.total_posts_with_location} 条带位置的动态</small>}
    </header>
    {!summary ? <Skeleton height={240} /> : <>
      <PlacesMap points={summary.points} />
      <ul className={styles.places}>{summary.points.slice(0, LISTED).map((point) => <li key={`${point.label}-${point.latitude}-${point.longitude}`}>
        <span><strong>{point.label}</strong><small>{point.count} 次 · {point.sources?.map(providerLabel).join("、") || "导入"} · {formatDate(point.last_seen)}</small></span>
        {point.map_url && <a href={point.map_url} target="_blank" rel="noreferrer" aria-label={`在地图中打开 ${point.label}`}><ExternalLink size={13} /></a>}
      </li>)}</ul>
      {summary.points.length > LISTED && <Faint>地图包含全部 {summary.points.length} 个地点。</Faint>}
    </>}
  </Panel>;
}
