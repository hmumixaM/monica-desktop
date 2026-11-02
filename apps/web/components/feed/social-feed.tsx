"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { monthLabel } from "../../lib/months";
import { ButtonLink, Button } from "../ui/button";
import { LoadMore } from "../ui/load-more";
import { Select } from "../ui/field";
import { Segmented } from "../ui/segmented";
import { EventDialog } from "./event-dialog";
import { FeedCard } from "./feed-card";
import styles from "./feed.module.css";
import { Masonry } from "./masonry";
import { FEED_SOURCES, type FeedEvent } from "./types";

const MONTH = /^\d{4}-\d{2}$/;
const MONTH_CHOICES = 60;

/** The last five years of months, newest first, plus the linked month if it is older. */
function recentMonths(linked: string | null): string[] {
  const months: string[] = [];
  const date = new Date();
  for (let n = 0; n < MONTH_CHOICES; n++, date.setMonth(date.getMonth() - 1)) {
    months.push(`${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}`);
  }
  return linked && MONTH.test(linked) && !months.includes(linked) ? [...months, linked] : months;
}

/**
 * The masonry feed with source filter, `?provider=` / `?month=` scoping and `?event=` deep links.
 * `preview` shows one short page with a link to the full feed (the profile tab, or /timeline).
 */
export function SocialFeed({ profileId, preview, sticky = false, title }: { profileId?: string; preview?: number; sticky?: boolean; title?: string }) {
  const search = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const [provider, setLocalProvider] = useState(search.get("provider") || "all");
  const month = search.get("month");
  // The full feed keeps its filters in the URL; a preview only filters itself.
  function scope(name: "provider" | "month", value: string | null) {
    const next = new URLSearchParams(search);
    if (value && value !== "all") next.set(name, value); else next.delete(name);
    next.delete("event");
    router.replace(`${pathname}${next.size ? `?${next}` : ""}`, { scroll: false });
  }
  function setProvider(value: string) {
    setLocalProvider(value);
    if (!preview) scope("provider", value);
  }
  useEffect(() => { setLocalProvider(search.get("provider") || "all"); }, [search]);
  const [events, setEvents] = useState<FeedEvent[]>([]);
  const [loading, setLoading] = useState(false);
  const [more, setMore] = useState(false);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<{ event: FeedEvent; origin: HTMLElement | null } | null>(null);
  const version = useRef(0);
  const request = useRef<AbortController | null>(null);
  const busy = useRef(false);
  const pageSize = preview ?? 12;

  async function load(cursor?: string) {
    if (cursor && busy.current) return;
    request.current?.abort();
    const abort = new AbortController();
    request.current = abort;
    const current = ++version.current;
    busy.current = true;
    setLoading(true);
    setError("");
    const params = new URLSearchParams({ limit: String(pageSize) });
    if (profileId) params.set("profile_id", profileId);
    if (provider !== "all") params.set("provider", provider);
    if (cursor) params.set("cursor", cursor);
    if (month && MONTH.test(month)) {
      const start = new Date(`${month}-01T00:00:00Z`);
      const end = new Date(start);
      end.setUTCMonth(end.getUTCMonth() + 1);
      params.set("after", start.toISOString());
      params.set("before", end.toISOString());
    }
    try {
      const response = await fetch(`/api/v1/timeline?${params}`, { signal: abort.signal });
      if (!response.ok) throw new Error(String(response.status));
      const rows: FeedEvent[] = await response.json();
      if (current !== version.current) return;
      setEvents((previous) => cursor ? [...previous, ...rows.filter((row) => !previous.some((old) => old.id === row.id))] : rows);
      setMore(rows.length === pageSize);
      const eventId = new URLSearchParams(window.location.search).get("event");
      const target = rows.find((row) => row.id === eventId);
      if (target) setSelected({ event: target, origin: null });
      else if (eventId && !cursor) {
        const detail = await fetch(`/api/v1/timeline/${encodeURIComponent(eventId)}`);
        if (detail.ok && current === version.current) {
          const event = await detail.json();
          setSelected({ event: { ...event, profile_name: event.profile_display_name, media: event.media || [] }, origin: null });
        }
      }
    } catch {
      if (!abort.signal.aborted && current === version.current) setError("动态加载失败，请重试。");
    } finally {
      if (current === version.current) {
        busy.current = false;
        setLoading(false);
      }
    }
  }
  useEffect(() => {
    setEvents([]);
    setMore(false);
    void load();
    return () => {
      request.current?.abort();
      version.current++;
      busy.current = false;
    };
  }, [provider, profileId, month]);

  const fullHref = profileId ? `/profiles/${profileId}?tab=timeline` : "/timeline";

  return <section className={styles.feed} id="timeline" aria-label={title ?? "动态"} data-preview={preview ? "" : undefined}>
    <div className={styles.toolbar} data-sticky={sticky ? "" : undefined}>
      {title && <h2>{title}</h2>}
      <Segmented label="动态来源" size="sm" value={provider} onChange={setProvider}
        options={Object.entries(FEED_SOURCES).map(([value, label]) => ({ value, label }))} />
      {!preview && <Select aria-label="只看某个月" className={styles.month} value={month && MONTH.test(month) ? month : ""}
        onChange={(event) => scope("month", event.target.value || null)}>
        <option value="">全部月份</option>
        {recentMonths(month).map((value) => <option key={value} value={value}>{monthLabel(value)}</option>)}
      </Select>}    </div>
    {error && <p role="alert" className={styles.status}>{error} <Button size="sm" onClick={() => void load(events.at(-1)?.cursor)}>重试</Button></p>}
    <Masonry columns={preview ? "compact" : undefined}>
      {events.map((event) => <FeedCard key={event.id} event={event} onOpen={(item, origin) => setSelected({ event: item, origin })} />)}
    </Masonry>
    {loading && <p className={styles.status} role="status">正在读取动态…</p>}
    {!loading && !events.length && !error && <p className={styles.status}>此来源暂无已收录的动态。</p>}
    {more && (preview
      ? <div className={styles.more}><ButtonLink variant="ghost" href={fullHref}>查看全部动态</ButtonLink></div>
      : <LoadMore cursor={events.at(-1)?.cursor ?? ""} busy={loading} error={!!error}
          onLoad={() => void load(events.at(-1)?.cursor)} />)}
    {selected && <EventDialog key={selected.event.id} event={selected.event} origin={selected.origin} onClose={() => setSelected(null)} />}
  </section>;
}
