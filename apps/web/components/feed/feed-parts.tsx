"use client";

import { ArrowUpRight, AtSign, ImageIcon, MapPin, Music2, Play, Repeat2, Sparkles } from "lucide-react";
import { useEffect, useState } from "react";

import { localMediaUrl } from "../../lib/media";
import styles from "./feed.module.css";
import { type EventMetadata, type FeedMedia, isVideo, type SharedLink } from "./types";

export function FeedImage({ media, alt, original = false, zoomTarget = false }: { media: FeedMedia; alt: string; original?: boolean; zoomTarget?: boolean }) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [media.id, media.thumbnail_id]);
  if (isVideo(media)) return <VideoCover media={media} />;
  if (failed || !localMediaUrl(media.id)) return null;
  return <img src={localMediaUrl(original ? media.id : media.thumbnail_id || media.id)} width={media.width || 640} height={media.height || 480}
    alt={alt} loading={original ? "eager" : "lazy"} onError={() => setFailed(true)} data-zoom-target={zoomTarget ? "" : undefined} />;
}

export function Unavailable({ children = "图片尚未缓存或暂不可用" }: { children?: string }) {
  return <span className={styles.unavailable}><ImageIcon size={24} aria-hidden="true" />{children}</span>;
}

export function VideoCover({ media }: { media?: FeedMedia }) {
  const poster = localMediaUrl(media?.thumbnail_id || (media?.media_type?.includes("image") ? media.id : undefined));
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [poster]);
  const hasPoster = !!poster && !failed;
  const channels = media?.source_kind === "wechat_channels";
  return <span className={styles.video} data-poster={hasPoster ? "" : undefined}
    style={hasPoster && media?.width && media?.height ? { aspectRatio: `${media.width}/${media.height}` } : undefined}>
    {hasPoster && <img src={poster} width={640} height={480} alt="视频缩略图" loading="lazy" onError={() => setFailed(true)} />}
    <span className={styles.play}><Play size={26} fill="currentColor" aria-hidden="true" /></span>
    <span className={styles.videoLabel}><strong>{channels ? "视频号" : "视频"}</strong><small>{channels ? "视频封面" : "点击查看"}</small></span>
  </span>;
}

export function SharedLinkCard({ link, media }: { link: SharedLink; media?: FeedMedia }) {
  const url = URL.canParse(link.url) ? new URL(link.url) : null;
  if (!url || !["http:", "https:"].includes(url.protocol) || url.username || url.password) return null;
  return <a className={styles.sharedLink} href={url.href} target="_blank" rel="noopener noreferrer">
    {media?.id ? <span className={styles.sharedCover}><FeedImage media={media} alt="" /></span> : <span className={styles.sharedIcon}><ArrowUpRight size={20} /></span>}
    <span><strong>{link.title || url.hostname}</strong>{link.description && <small>{link.description}</small>}<small>{link.source_name || url.hostname}</small></span>
  </a>;
}

export function EventAnnotations({ metadata, detailed = false }: { metadata?: EventMetadata; detailed?: boolean }) {
  if (!metadata || (!metadata.locations.length && !metadata.mentions.length && !metadata.extras?.length)) return null;
  return <div className={styles.annotations} aria-label="事件信息">
    {metadata.extras?.map((item, index) => <div className={styles.annotation} key={index}>
      {item.kind === "music" ? <Music2 size={14} aria-hidden="true" /> : item.kind === "reshare" ? <Repeat2 size={14} aria-hidden="true" /> : <Sparkles size={14} aria-hidden="true" />}
      <div><small>{item.label}</small>{item.url ? <a href={item.url} target="_blank" rel="noopener noreferrer">{item.text}<ArrowUpRight size={12} /></a> : <span>{item.text}</span>}</div>
    </div>)}
    {metadata.locations.map((place, index) => <div className={styles.annotation} key={index}><MapPin size={14} aria-hidden="true" /><div>
      {place.map_url ? <a href={place.map_url} target="_blank" rel="noopener noreferrer" aria-label={`在地图查看 ${place.label}`}>{place.label}<ArrowUpRight size={12} /></a> : <span>{place.label}</span>}
      {detailed && place.location_id && <small>地点 ID：{place.location_id}</small>}
      {detailed && place.address && place.address !== place.label && <small>{place.address}</small>}
      {detailed && place.latitude !== undefined && place.longitude !== undefined && <small>{place.coordinate_source} · {place.latitude.toFixed(5)}, {place.longitude.toFixed(5)}</small>}
    </div></div>)}
    {!!metadata.mentions.length && <div className={styles.annotation}><AtSign size={14} aria-hidden="true" /><div className={styles.mentions}>
      <span className="sr-only">提及：</span>
      {metadata.mentions.map((mention, index) => mention.url
        ? <a key={index} href={mention.url} target={mention.url.startsWith("/") ? undefined : "_blank"} rel="noopener noreferrer">{mention.label}</a>
        : <span key={index}>{mention.label}</span>)}
    </div></div>}
  </div>;
}

export function ChannelsInfo({ media }: { media?: FeedMedia }) {
  if (media?.source_kind !== "wechat_channels") return null;
  const seconds = Math.floor((media.duration_ms || 0) / 1000);
  return <div className={styles.annotations}><div className={styles.annotation}><Play size={14} aria-hidden="true" /><div>
    <small>视频号{media.author_name ? ` · ${media.author_name}` : ""}{seconds > 0 ? ` · ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}` : ""}</small>
    {media.description && <span>{media.description}</span>}
  </div></div></div>;
}
