import type { InstagramPost } from "./instagram-post";

export type FeedMedia = {
  source_kind?: string; author_name?: string; description?: string; duration_ms?: number; width?: number; height?: number; id?: string;
  thumbnail_id?: string; source_url?: string; thumbnail_url?: string; media_type?: string; source_media_type?: string; ready?: boolean;
};
export type SharedLink = { url: string; title?: string; description?: string; source_name?: string };
export type EventPlace = { label: string; location_id?: string; address?: string; latitude?: number; longitude?: number; map_url?: string; coordinate_source?: string };
export type EventMetadata = {
  post?: InstagramPost | null; extras?: { kind: string; label: string; text: string; url?: string }[]; event_type: string;
  locations: EventPlace[]; mentions: { label: string; external_id: string; url?: string }[];
};
export type FeedEvent = {
  event_metadata?: EventMetadata; shared_link?: SharedLink; id: string; profile_id?: string; profile_name?: string; avatar_media_id?: string;
  provider: string; title: string; occurred_at?: string; summary?: string; cursor: string; media: FeedMedia[]; like_count?: number; comment_count?: number;
};
export type EventDetail = {
  event_metadata?: EventMetadata; content?: unknown; summary?: string; participants?: { id: string; display_name: string }[]; media?: FeedMedia[];
  interactions?: { id: string; interaction_type: string; author_name?: string; author_profile_id?: string; content?: { text?: string }; media?: FeedMedia[] }[];
  media_reading?: MediaReading | null;
};

/** What the model read off the post's images: one line for the post, and text and content per image. */
export type MediaReading = {
  summary: string;
  items: { index: number; kind: "photo" | "video"; category: string; text: string; description: string }[];
};

export const FEED_SOURCES: Record<string, string> = { all: "全部", wechat: "微信", instagram: "Instagram", linkedin: "LinkedIn", manual: "活动" };

export const isVideo = (media: FeedMedia) => (media.source_media_type || media.media_type || "").includes("video");

// WeChat and Instagram record the posting minute; LinkedIn only gives relative ages ("2w"), so it keeps the day.
const MINUTE_PRECISE = new Set(["wechat", "instagram"]);
export const eventDate = (value?: string, provider?: string) => value ? new Intl.DateTimeFormat("zh-CN",
  MINUTE_PRECISE.has(provider ?? "") ? { dateStyle: "medium", timeStyle: "short" } : { dateStyle: "medium" }).format(new Date(value)) : "日期未收录";

export function eventKind(event: Pick<FeedEvent, "provider" | "media" | "event_metadata">) {
  if (event.event_metadata?.event_type === "story") return "Instagram Story";
  if (event.provider === "wechat") return event.media?.some((media) => media.source_kind === "wechat_channels") ? "视频号 · 朋友圈" : "朋友圈";
  return FEED_SOURCES[event.provider] || event.provider;
}
