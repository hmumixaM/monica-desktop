"use client";

import { Heart, ImageIcon, MessageCircle } from "lucide-react";

import { recordText } from "../../lib/record-text";
import { PersonLink } from "../ui/person-link";
import { Avatar } from "../ui/primitives";
import { ChannelsInfo, EventAnnotations, FeedImage, SharedLinkCard } from "./feed-parts";
import styles from "./feed.module.css";
import { InstagramPostBadge } from "./instagram-post";
import { eventDate, eventKind, type FeedEvent } from "./types";

/** One post. Clicking anywhere that is not a link or control opens the detail; the photo zooms from its thumbnail. */
export function FeedCard({ event, onOpen }: { event: FeedEvent; onOpen: (event: FeedEvent, origin: HTMLElement | null) => void }) {
  const caption = recordText(event.summary);
  const photo = !event.shared_link && event.media?.length > 0 && event.media[0].ready !== false;
  return <article className={styles.card} data-text={photo ? undefined : ""} data-source={event.provider}
    onClick={(click) => {
      const target = click.target as HTMLElement;
      if (target.closest("a,button,video,summary")) return;
      onOpen(event, (click.currentTarget as HTMLElement).querySelector("[data-thumb] img"));
    }}>
    {photo && <button type="button" className={styles.photo} data-thumb="" aria-label={`打开${event.profile_name || ""}的${event.title}`}
      onClick={(click) => onOpen(event, click.currentTarget.querySelector("img"))}>
      <FeedImage media={event.media[0]} alt={caption.slice(0, 80) || event.title} />
      {event.media.length > 1 && <span className={styles.count}><ImageIcon size={12} aria-hidden="true" />{event.media.length}</span>}
    </button>}
    <header className={styles.cardHeader}>
      {event.profile_id ? <PersonLink className={styles.person} href={`/profiles/${event.profile_id}`}>
        <Avatar name={event.profile_name || "友"} mediaId={event.avatar_media_id} size={26} round />
        <strong>{event.profile_name || "查看联系人"}</strong>
      </PersonLink> : <strong className={styles.person}>{event.title}</strong>}
      <span className={styles.kind}>{eventKind(event)}</span>
    </header>
    {caption && <button type="button" className={styles.caption} onClick={() => onOpen(event, null)}><p>{caption}</p></button>}
    {event.shared_link && <SharedLinkCard link={event.shared_link} media={event.media?.[0]} />}
    <ChannelsInfo media={event.media?.[0]} />
    <EventAnnotations metadata={event.event_metadata} />
    <InstagramPostBadge post={event.event_metadata?.post} />
    <footer className={styles.cardFooter}>
      <time dateTime={event.occurred_at}>{eventDate(event.occurred_at, event.provider)}</time>
      {(event.like_count ?? 0) > 0 && <span><Heart size={13} aria-hidden="true" />{event.like_count}</span>}
      {(event.comment_count ?? 0) > 0 && <span><MessageCircle size={13} aria-hidden="true" />{event.comment_count}</span>}
    </footer>
  </article>;
}
