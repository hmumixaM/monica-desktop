"use client";

import { useInfiniteQuery } from "@tanstack/react-query";
import { Search, X } from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";

import { monthLabel } from "../../lib/months";
import { fetchJson, queryKeys } from "../../lib/queries";
import { Button, IconButton } from "../ui/button";
import { SearchInput } from "../ui/field";
import { LoadMore } from "../ui/load-more";
import { Failure, Loading, Muted } from "../ui/primitives";
import styles from "./conversation.module.css";

type Message = { id: string; occurred_at: string; message_type: string; text?: string; sender_profile_id?: string; sender_name?: string };
const PAGE = 50;
const KIND: Record<string, string> = { "3": "图片", "34": "语音", "43": "视频", "47": "表情", "49": "分享", "10000": "系统消息" };

/** The readable body; a reply carries its own text in the XML title and the quoted message as plain text before it. */
function readable(row: Message): { text: string; quote?: string } {
  const fallback = `[${KIND[row.message_type] || "消息"}]`;
  const raw = row.message_type === "1" ? (row.text || "空消息").replace(/^[A-Za-z][\w-]{4,40}:\n/, "") : row.text || "";
  const xmlAt = raw.search(/<\?xml|<msg[\s>]/);
  if (xmlAt < 0) return { text: raw || fallback };
  const doc = new DOMParser().parseFromString(raw.slice(xmlAt), "text/xml");
  const title = doc.querySelector("title")?.textContent?.trim() || doc.querySelector("content")?.textContent?.trim();
  const quote = raw.slice(0, xmlAt).trim();
  return title ? { text: title, quote: quote || undefined } : { text: quote || fallback };
}

const TIME_GAP = 10 * 60_000;

const dayKey = (value: string) => new Date(value).toLocaleDateString("sv-SE");
const dayLabel = new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "long", day: "numeric", weekday: "short" });
const clock = new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" });

/**
 * One conversation, newest first, grouped under sticky day headings. `counterpartId` is the other person in a
 * direct chat: everything they did not send is drawn as my bubble. `?month=` (with `?timezone=`) narrows to a month.
 */
export function Conversation({ profileId, conversationId, counterpartId, heading }: {
  profileId?: string; conversationId?: string; counterpartId?: string; heading?: ReactNode;
}) {
  const params = useSearchParams();
  const month = params.get("month") ?? undefined;
  const anchor = params.get("message") ?? undefined;
  const timezone = params.get("timezone") || "UTC";
  const [searching, setSearching] = useState(false);
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState("");
  const input = useRef<HTMLInputElement>(null);
  const filters = { message: anchor, q: query || undefined, month, timezone: month ? timezone : undefined };
  const base = conversationId ? `/api/v1/conversations/${conversationId}/messages` : `/api/v1/profiles/${profileId}/messages`;
  const messages = useInfiniteQuery({
    queryKey: conversationId ? queryKeys.messages(conversationId, filters) : queryKeys.profileTab(profileId!, "messages", filters),
    queryFn: ({ pageParam, signal }) => {
      const search = new URLSearchParams({ limit: String(PAGE) });
      for (const [key, value] of Object.entries(filters)) if (value) search.set(key, value);
      if (pageParam) {
        search.set("before", pageParam.before);
        search.set("before_id", pageParam.beforeId);
      }
      return fetchJson<Message[]>(`${base}?${search}`, { signal });
    },
    initialPageParam: undefined as { before: string; beforeId: string } | undefined,
    getNextPageParam: (page) => page.length === PAGE ? { before: page.at(-1)!.occurred_at, beforeId: page.at(-1)!.id } : undefined,
  });
  const rows = useMemo(() => {
    const seen = new Set<string>();
    return (messages.data?.pages.flat() ?? []).filter((row) => !seen.has(row.id) && !!seen.add(row.id));
  }, [messages.data]);
  const positioned = useRef<string | null>(null);
  useEffect(() => {
    if (anchor && positioned.current !== anchor && rows.some((row) => row.id === anchor)) {
      positioned.current = anchor;
      document.getElementById(`message-${anchor}`)?.scrollIntoView({ block: "center" });
    }
  }, [anchor, rows]);
  const days = useMemo(() => rows.reduce<{ key: string; rows: Message[] }[]>((groups, row) => {
    const key = dayKey(row.occurred_at);
    if (groups.at(-1)?.key === key) groups.at(-1)!.rows.push(row);
    else groups.push({ key, rows: [row] });
    return groups;
  }, []), [rows]);
  const allMonths = profileId ? `/profiles/${profileId}?tab=messages` : `/messages?conversation=${conversationId}`;
  const group = !counterpartId;

  function openSearch() {
    setSearching(true);
    requestAnimationFrame(() => input.current?.focus());
  }
  function closeSearch() {
    setSearching(false);
    setDraft("");
    setQuery("");
  }

  return <section className={styles.conversation} aria-label="聊天记录">
    <header className={styles.header}>
      {searching
        ? <form className={styles.search} role="search" onSubmit={(event) => { event.preventDefault(); setQuery(draft.trim()); }}>
          <SearchInput ref={input} value={draft} onChange={(event) => setDraft(event.target.value)} placeholder="在会话中搜索…" aria-label="会话内搜索"
            onKeyDown={(event) => { if (event.key === "Escape") closeSearch(); }} />
          <Button type="submit" size="sm" variant="primary">搜索</Button>
          <IconButton size="sm" label="关闭搜索" onClick={closeSearch}><X size={15} /></IconButton>
        </form>
        : <>
          <div className={styles.heading}>{heading ?? <h2>聊天记录</h2>}</div>
          <IconButton label="在会话中搜索" onClick={openSearch}><Search size={17} /></IconButton>
        </>}
    </header>
    {(month || query) && <p className={styles.filter}>
      {month && <span><strong>{monthLabel(month)}</strong><small>{timezone}</small></span>}
      {query && <span>包含「<strong>{query}</strong>」</span>}
      {month && <Link href={allMonths} scroll={false}>全部月份</Link>}
      {query && <button type="button" onClick={closeSearch}>清除搜索</button>}
    </p>}
    {messages.isError && <Failure onRetry={() => void messages.refetch()}>聊天记录读取失败。</Failure>}
    <div className={styles.thread} data-group={group ? "" : undefined}>
      {days.map((day) => <section key={day.key} aria-label={dayLabel.format(new Date(day.rows[0].occurred_at))}>
        <h3 className={styles.day}><span>{dayLabel.format(new Date(day.rows[0].occurred_at))}</span></h3>
        {day.rows.map((row, index) => {
          const system = row.message_type === "10000";
          const mine = !!counterpartId && row.sender_profile_id !== counterpartId;
          const above = day.rows[index - 1];
          const sameSender = !!above && above.message_type !== "10000" && above.sender_profile_id === row.sender_profile_id && above.sender_name === row.sender_name;
          const below = day.rows[index + 1];
          const lastOfRun = !below || below.sender_profile_id !== row.sender_profile_id || below.sender_name !== row.sender_name
            || new Date(row.occurred_at).getTime() - new Date(below.occurred_at).getTime() > TIME_GAP;
          const body = readable(row);
          return <article id={`message-${row.id}`} data-highlighted={row.id === anchor ? "" : undefined} key={row.id} className={styles.message} data-mine={mine ? "" : undefined} data-system={system ? "" : undefined}
            data-continued={sameSender ? "" : undefined} title={clock.format(new Date(row.occurred_at))}>
            {group && !system && !sameSender && <header>{row.sender_profile_id
              ? <Link href={`/profiles/${row.sender_profile_id}`}>{row.sender_name || "联系人"}</Link>
              : row.sender_name ? <Link href={`/linking?q=${encodeURIComponent(row.sender_name)}`} title="发送者尚未关联资料，去关联">{row.sender_name}</Link>
              : <span>发送者未关联</span>}</header>}
            <p>{body.quote && <q>{body.quote}</q>}{body.text}</p>
            {lastOfRun && !system && <time dateTime={row.occurred_at}>{clock.format(new Date(row.occurred_at))}</time>}
          </article>;
        })}
      </section>)}
    </div>
    {messages.isPending && <Loading />}
    {messages.isSuccess && !rows.length && <Muted>{query ? "没有找到匹配的消息。" : "尚无符合条件的已收录消息。"}</Muted>}
    {messages.hasNextPage && <LoadMore cursor={rows.at(-1)?.id ?? ""} busy={messages.isFetchingNextPage} error={messages.isError}
      onLoad={() => void messages.fetchNextPage()} label="更早的消息" />}
  </section>;
}
