"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import { apiFetch } from "../../lib/api";
import type { PersonRef } from "../../lib/api-types";
import { Button } from "../ui/button";
import { LoadMore } from "../ui/load-more";
import { Dialog } from "../ui/dialog";
import { Select, SearchInput } from "../ui/field";
import { Failure, Faint, Loading, Muted } from "../ui/primitives";
import styles from "./dialogs.module.css";

type SpeechMessage = { time?: string; sender?: string; text?: string };
type SpeechRoom = { group_id?: string | null; name?: string; chat?: string; messages?: SpeechMessage[]; error?: string; partial?: boolean; next_window?: number | null };

/** Reads this person's group messages live from Chatlog; nothing is written to Monica. */
export function GroupSpeechDialog({ profileId, groups, onClose }: { profileId: string; groups: PersonRef[]; onClose: () => void }) {
  const request = useRef(0);
  const [query, setQuery] = useState("");
  const [groupId, setGroupId] = useState("");
  const [rooms, setRooms] = useState<SpeechRoom[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [nextOffset, setNextOffset] = useState<number | null>(null);

  const [active, setActive] = useState({query: "", group: ""});

  async function load(search = query, room = groupId, offset = 0, window = 0, resume = false) {
    const current = ++request.current;
    setBusy(true);
    setError("");
    const params = new URLSearchParams({ limit: "50", group_offset: String(offset), window_index: String(window) });
    if (search.trim()) params.set("q", search.trim());
    if (room) params.set("group_id", room);
    try {
      const response = await apiFetch(`/api/v1/profiles/${profileId}/group-speech?${params}`);
      const body = await response.json().catch(() => ({}));
      if (current !== request.current) return;
      if (!response.ok) {
        setError(typeof body.detail === "string" ? body.detail : "群聊记录读取失败");
        return;
      }
      if (resume) {
        setRooms(previous => previous.map(item => {
          const next = (body.groups as SpeechRoom[] ?? []).find(value => value.group_id === item.group_id);
          if (!next) return item;
          const messages = new Map([...(item.messages ?? []), ...(next.messages ?? [])].map(message => [`${message.time}:${message.text}`, message]));
          return {...next, messages: Array.from(messages.values())};
        }));
      } else {
        setRooms(previous => offset ? [...previous, ...(body.groups ?? [])] : body.groups ?? []);
        setNextOffset(body.next_group_offset ?? null);
        setActive({query: search, group: room});
      }
    } catch {
      if (current === request.current) setError("群聊记录读取失败");
    } finally {
      if (current === request.current) setBusy(false);
    }
  }
  useEffect(() => { void load("", ""); }, [profileId]);

  const spoken = rooms.filter((room) => room.messages?.length || room.error);
  const quiet = rooms.length - spoken.length;
  const partial = rooms.find((room) => room.partial && room.group_id && room.next_window != null);
  return <Dialog title="群聊发言" size="wide" onClose={onClose} closeLabel="关闭群聊记录">
    <form className={styles.speechForm} onSubmit={(event) => { event.preventDefault(); void load(); }}>
      {groups.length > 0 && <Select aria-label="选择群" value={groupId} onChange={(event) => setGroupId(event.target.value)}>
        <option value="">全部群</option>
        {groups.map((group) => <option key={group.profile_id} value={group.profile_id}>{group.name}</option>)}
      </Select>}
      <SearchInput aria-label="在发言里搜索" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="关键词…" />
      <Button type="submit" variant="primary" disabled={busy}>{busy ? "读取中…" : "获取"}</Button>
    </form>
    {error && <Failure>{error}</Failure>}
    {busy && !rooms.length && <Loading>正在读取群聊，请稍等…</Loading>}
    {spoken.map((room, index) => <section className={styles.room} key={`${room.chat ?? room.group_id ?? room.name}-${index}`}>
      <h3>{room.group_id ? <Link href={`/profiles/${room.group_id}`}>{room.name}</Link> : room.name}</h3>
      {room.error && <Failure>{room.error}</Failure>}
      <ol>{room.messages?.map((message, position) => <li key={`${message.time}-${position}`}>
        <header><strong>{message.sender || "群成员"}</strong><time>{message.time}</time></header>
        <p>{message.text || "（空消息）"}</p>
      </li>)}</ol>
    </section>)}
    {(partial || nextOffset !== null) && <LoadMore cursor={partial ? `${partial.group_id}:${partial.next_window}` : String(nextOffset)} busy={busy} error={!!error}
      onLoad={() => partial ? void load(active.query, partial.group_id!, 0, partial.next_window!, true)
        : void load(active.query, active.group, nextOffset!)} />}
    {!busy && rooms.length > 0 && !spoken.length && <Muted>最近的群记录里没有这个人的发言。</Muted>}
    {!busy && quiet > 0 && <Faint>另外 {quiet} 个群里没有找到发言。</Faint>}
  </Dialog>;
}
