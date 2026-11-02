import { ArrowUpRight, AtSign } from "lucide-react";

import styles from "./feed.module.css";

type User = { username: string; external_id?: string; name?: string; url?: string; media_index?: number };
export type InstagramPost = { post_id?: string; shortcode?: string; permalink?: string; published_at?: string; media_type?: string; tagged_users?: User[] };
type SourceMedia = {
  external_id?: string; media_type?: string; source_url?: string; thumbnail_url?: string; width?: number; height?: number; duration_ms?: number;
  accessibility_caption?: string; image_resources?: { url: string; width?: number; height?: number }[]; video_resources?: { url: string; width?: number; height?: number }[];
};
type Content = InstagramPost & {
  like_count?: number | null; comment_count?: number | null; media?: SourceMedia[];
  people_preview?: { commenters?: User[]; repliers?: User[]; likers?: User[] };
};

const TYPES: Record<string, string> = { image: "图片", video: "视频", carousel: "轮播" };

function safeUrl(value?: string) {
  if (!value || !URL.canParse(value)) return undefined;
  const url = new URL(value);
  return ["https:", "http:"].includes(url.protocol) && !url.username && !url.password ? url.href : undefined;
}
const userUrl = (name: string) => /^[A-Za-z0-9_.]{1,30}$/.test(name) ? `https://www.instagram.com/${name}/` : undefined;
const original = (post: InstagramPost) => post.shortcode && /^[A-Za-z0-9_-]+$/.test(post.shortcode) ? `https://www.instagram.com/p/${post.shortcode}/` : safeUrl(post.permalink);

export function InstagramPostBadge({ post }: { post?: InstagramPost | null }) {
  if (!post) return null;
  const tagged = Array.from(new Map((post.tagged_users ?? []).map((user) => [user.username, user])).values());
  return <div className={styles.igBadge}>
    <span>{TYPES[post.media_type || ""] || "帖子"}</span>
    {original(post) && <a href={original(post)} target="_blank" rel="noopener noreferrer">Instagram 原帖<ArrowUpRight size={12} /></a>}
    {tagged.length > 0 && <div className={styles.igTagged}><AtSign size={13} aria-hidden="true" /><span>标记：</span>
      {tagged.slice(0, 8).map((user) => <a href={userUrl(user.username)} key={user.username} target="_blank" rel="noopener noreferrer">@{user.username}</a>)}
      {tagged.length > 8 && <small>更多标记见原帖信息</small>}
    </div>}
  </div>;
}

/** The post's link and tags up front; identifiers, counts and source URLs folded away for when they are needed. */
export function InstagramPostDetails({ post, content }: { post?: InstagramPost | null; content?: unknown }) {
  if (!post) return null;
  const value = (content && typeof content === "object" ? content : {}) as Content;
  const media = value.media || [];
  const people = (["commenters", "repliers", "likers"] as const).flatMap((key) => value.people_preview?.[key] ?? []);
  return <section className={styles.igDetails} aria-label="Instagram 帖子信息">
    <InstagramPostBadge post={post} />
    <details><summary>原帖信息</summary>
      <dl>
        {post.post_id && <><dt>Post ID</dt><dd>{post.post_id}</dd></>}
        {post.shortcode && <><dt>Shortcode</dt><dd>{post.shortcode}</dd></>}
        {(value.like_count != null || value.comment_count != null) && <><dt>赞 / 评论</dt><dd>{value.like_count ?? "—"} / {value.comment_count ?? "—"}</dd></>}
      </dl>
      {(post.tagged_users?.length ?? 0) > 8 && <div className={styles.igTagged}>{post.tagged_users!.map((user, index) =>
        <a key={index} href={userUrl(user.username)} target="_blank" rel="noopener noreferrer">@{user.username}{user.media_index ? ` · 第 ${user.media_index} 项` : ""}</a>)}</div>}
      {people.length > 0 && <div className={styles.igTagged}><span>互动过：</span>{people.map((user, index) =>
        <a key={index} href={userUrl(user.username)} target="_blank" rel="noopener noreferrer">@{user.username}</a>)}</div>}
      {media.length > 0 && <>
      <p className={styles.igNote}>页面预览使用本地归档。以下为 {media.length} 项媒体的来源地址，可能过期。</p>
      <ol className={styles.igMedia}>{media.map((item, index) => <li key={index}>
        <strong>第 {index + 1} 项 · {TYPES[item.media_type || ""] || item.media_type || "媒体"}</strong>
        <div>{item.external_id && <span>媒体 ID：{item.external_id} </span>}{item.width && item.height && <span>{item.width} × {item.height} </span>}
          {item.duration_ms != null && <span>{(item.duration_ms / 1000).toFixed(1)} 秒</span>}</div>
        {item.accessibility_caption && <p>{item.accessibility_caption}</p>}
        <div className={styles.igLinks}>
          {safeUrl(item.source_url) && <a href={safeUrl(item.source_url)} target="_blank" rel="noopener noreferrer">原始{item.media_type === "video" ? "视频" : "图片"} URL ↗</a>}
          {item.thumbnail_url !== item.source_url && safeUrl(item.thumbnail_url) && <a href={safeUrl(item.thumbnail_url)} target="_blank" rel="noopener noreferrer">缩略图 URL ↗</a>}
          {[...(item.image_resources || []), ...(item.video_resources || [])].filter((resource) => safeUrl(resource.url)).map((resource, k) =>
            <a key={k} href={safeUrl(resource.url)} target="_blank" rel="noopener noreferrer">来源版本 {resource.width && resource.height ? `${resource.width} × ${resource.height}` : k + 1} ↗</a>)}
        </div>
      </li>)}</ol></>}
    </details>
  </section>;
}
