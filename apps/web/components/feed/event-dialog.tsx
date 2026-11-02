"use client";

import { Heart, MessageCircle, X } from "lucide-react";
import Link from "next/link";
import { type CSSProperties, useEffect, useRef, useState } from "react";

import { localMediaUrl } from "../../lib/media";
import { recordText } from "../../lib/record-text";
import { useZoomFrom } from "../../lib/zoom";
import { PhotoPeople } from "../faces/photo-people";
import { IconButton } from "../ui/button";
import { PersonLink } from "../ui/person-link";
import { Avatar, SourceMark } from "../ui/primitives";
import { ChannelsInfo, EventAnnotations, FeedImage, SharedLinkCard, Unavailable, VideoCover } from "./feed-parts";
import styles from "./feed.module.css";
import { InstagramPostDetails } from "./instagram-post";
import { type EventDetail, eventDate, eventKind, type FeedEvent, isVideo, type MediaReading, type SharedLink } from "./types";

/** Post detail: photos on the left, text, places, tags and interactions on the right. Mounted only while open. */
export function EventDialog({ event, origin, onClose }: { event: FeedEvent; origin: HTMLElement | null; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [detail, setDetail] = useState<EventDetail | null>(null);
  const [error, setError] = useState("");
  const close = useZoomFrom(dialog, origin);

  useEffect(() => { dialog.current?.showModal(); }, []);
  useEffect(() => {
    const abort = new AbortController();
    setDetail(null);
    setError("");
    fetch(`/api/v1/timeline/${event.id}`, { signal: abort.signal }).then(async (response) => {
      if (!response.ok) throw new Error(String(response.status));
      setDetail(await response.json());
    }).catch((reason: Error) => { if (reason.name !== "AbortError") setError("详情加载失败，请关闭后重试。"); });
    return () => abort.abort();
  }, [event.id]);

  const metadata = detail?.event_metadata || event.event_metadata;
  const media = detail?.media || event.media || [];
  const link = (detail?.content as { shared_link?: SharedLink } | undefined)?.shared_link || event.shared_link;
  const body = recordText(detail?.content) || recordText(detail?.summary || event.summary);
  const interactions = detail?.interactions?.filter((item) => item.interaction_type !== "mention") ?? [];
  const hasPhotos = !link && media.length > 0;
  const ratio = media[0]?.width && media[0]?.height ? media[0].width / media[0].height
    : metadata?.event_type === "story" ? 9 / 16 : undefined;

  return <dialog ref={dialog} className={styles.detail} data-photos={hasPhotos ? "" : undefined} data-source={event.provider}
    style={ratio ? { "--photo-ratio": ratio } as CSSProperties : undefined} aria-label="动态详情" onClose={onClose}
    onCancel={(cancel) => { cancel.preventDefault(); close(); }}
    onClick={(click) => { if (click.target === click.currentTarget) close(); }}>
    {hasPhotos && <div className={styles.detailPhotos}>
      {media.map((item, index) => localMediaUrl(item.id)
        ? <a href={localMediaUrl(item.id)} target="_blank" rel="noreferrer" aria-label={`查看原图 ${index + 1}`} key={item.id}>
          {item.media_type?.includes("video")
            ? <video controls preload="metadata" src={localMediaUrl(item.id)} poster={localMediaUrl(item.thumbnail_id)} data-zoom-target={index === 0 ? "" : undefined} />
            : <FeedImage media={item} original alt={`动态照片 ${index + 1}`} zoomTarget={index === 0} />}
        </a>
        : <span key={index}>{isVideo(item) ? <VideoCover media={item} /> : <Unavailable />}</span>)}
    </div>}
    <header className={styles.detailHeader}>
      {event.profile_id ? <PersonLink className={styles.person} href={`/profiles/${event.profile_id}`}>
        <Avatar name={event.profile_name || event.title} mediaId={event.avatar_media_id} size={36} round />
        <span><strong>{event.profile_name || event.title}</strong><small><SourceMark provider={event.provider} size={11} /> {eventKind({ ...event, event_metadata: metadata })} · <time dateTime={event.occurred_at}>{eventDate(event.occurred_at, event.provider)}</time></small></span>
      </PersonLink> : <span className={styles.person}><span><strong>{event.title}</strong><small>{eventKind(event)} · {eventDate(event.occurred_at, event.provider)}</small></span></span>}
      <IconButton label="关闭动态详情" onClick={close}><X size={18} /></IconButton>
    </header>
    <div className={styles.detailText}>
      {link && <SharedLinkCard link={link} media={media[0]} />}
      {body && <p className={styles.body}>{body}</p>}
      <ChannelsInfo media={media[0]} />
      {hasPhotos && <PhotoPeople mediaIds={media.flatMap((item) => !isVideo(item) && item.id && localMediaUrl(item.id) ? [item.id] : [])} />}
      <EventAnnotations metadata={metadata} detailed />
      <InstagramPostDetails post={metadata?.post} content={detail?.content} />
      {detail?.media_reading && <MediaReadingNote reading={detail.media_reading} />}
      {!!detail?.participants?.length && <div className={styles.participants}>{detail.participants.map((person) =>
        <Link key={person.id} href={`/profiles/${person.id}`}>{person.display_name}</Link>)}</div>}
      {!detail && !error && <p className={styles.status} role="status">正在读取详情…</p>}
      {error && <p role="alert">{error}</p>}
      {interactions.length > 0 && <section className={styles.replies}>
        <h3>赞与评论</h3>
        {interactions.map((item) => <div key={item.id}>
          {item.interaction_type === "like" ? <Heart size={13} aria-hidden="true" /> : <MessageCircle size={13} aria-hidden="true" />}
          {item.author_profile_id ? <Link href={`/profiles/${item.author_profile_id}`}>{item.author_name || "查看联系人"}</Link> : <strong>{item.author_name || "用户"}</strong>}
          {item.content?.text && <span>{item.content.text}</span>}
          {!!item.media?.length && <div className={styles.commentPhotos}>
            {item.media.map((image, index) => localMediaUrl(image.id)
              ? <a key={image.id} href={localMediaUrl(image.id)} target="_blank" rel="noreferrer" aria-label={`查看评论原图 ${index + 1}`}>
                <FeedImage media={image} original alt={`评论图片 ${index + 1}`} />
              </a>
              : <span key={index} role="status">评论图片待下载</span>)}
          </div>}
        </div>)}
      </section>}
    </div>
  </dialog>;
}

/** Keep the recognition entry next to the post or Story's own information. */
function MediaReadingNote({ reading }: { reading: MediaReading }) {
  return <details className={styles.reading}>
    <summary>图片识别</summary>
    <p>{reading.summary}</p>
    <ol>{reading.items.map((item) => <li key={item.index}>
      <span>{item.category}</span>{item.description}{item.text && <q>{item.text}</q>}
    </li>)}</ol>
  </details>;
}
