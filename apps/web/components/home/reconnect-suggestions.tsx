"use client";

import { AlarmClock, Check, Copy, MessageCircleHeart, Shuffle, X } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { type ReconnectAction, type ReconnectPerson, useReconnectAction } from "../../lib/ai";
import { Button, IconButton } from "../ui/button";
import { Menu } from "../ui/menu";
import { PersonLink } from "../ui/person-link";
import { Avatar, relativeDays } from "../ui/primitives";
import { useToast } from "../ui/toast";
import styles from "./reconnect.module.css";

const DONE: Record<ReconnectAction["state"], string> = { done: "已标记为联系过", snoozed: "稍后再提醒", dismissed: "不再提醒" };

function Suggestion({ person }: { person: ReconnectPerson }) {
  const act = useReconnectAction();
  const toast = useToast();
  const run = (action: ReconnectAction) => act.mutate({ person, action }, {
    onSuccess: () => toast.show({
      message: `${person.name}：${action.state === "snoozed" ? (action.snooze === "2w" ? "两周后再提醒" : "一个月后再提醒") : DONE[action.state]}`,
      actionLabel: "撤销",
      onAction: () => act.mutate({ person, action: null }),
    }),
    onError: () => toast.show({ message: "没能保存，请重试" }),
  });
  const copy = (text: string) => void navigator.clipboard.writeText(text).then(() => toast.show({ message: "已复制开场白" }));
  const posts = person.suggestion?.sources.filter((source) => source.kind === "post") ?? [];
  return <li className={styles.item}>
    <PersonLink className={styles.who} href={`/profiles/${person.profile_id}`}>
      <Avatar name={person.name} mediaId={person.avatar_media_id} size={40} round />
      <span><strong>{person.name}</strong><small>上次联系 {relativeDays(person.last_contact_at)}</small></span>
    </PersonLink>
    <div className={styles.actions}>
      <Button size="sm" variant="secondary" onClick={() => run({ state: "done" })} disabled={act.isPending}><Check size={14} />已联系</Button>
      <Menu trigger={<IconButton size="sm" label="稍后提醒" disabled={act.isPending}><AlarmClock size={15} /></IconButton>} items={[
        { label: "两周后", onSelect: () => run({ state: "snoozed", snooze: "2w" }) },
        { label: "一个月后", onSelect: () => run({ state: "snoozed", snooze: "1m" }) },
      ]} />
      <IconButton size="sm" label="不再提醒" disabled={act.isPending} onClick={() => run({ state: "dismissed" })}><X size={15} /></IconButton>
    </div>
    {person.suggestion ? <div className={styles.why}>
      <p>{person.suggestion.reason}
        {posts.map((source, index) => <Link key={source.id} className={styles.source} href={`/timeline?event=${source.id}`}>
          {posts.length > 1 ? `动态 ${index + 1}` : "看动态"}
        </Link>)}
      </p>
      <p className={styles.opener}>
        <span>“{person.suggestion.opener}”</span>
        <IconButton size="sm" label="复制开场白" onClick={() => copy(person.suggestion!.opener)}><Copy size={13} /></IconButton>
      </p>
    </div> : <p className={styles.why}>以前一年最多聊过 {person.peak} 条，理由生成后会显示在这里。</p>}
  </li>;
}

/** Model suggestions of who to get back in touch with, a few at a time from a shuffled draw; each can be done,
 * snoozed or dismissed, with undo. 换一批 shows the next few, and draws again once the list runs out. */
export function ReconnectSuggestions({ people, onRedraw, redrawing, shown = 5 }: {
  people: ReconnectPerson[]; onRedraw: () => void; redrawing: boolean; shown?: number;
}) {
  const [offset, setOffset] = useState(0);
  const start = offset < people.length ? offset : 0;
  const next = () => {
    if (start + shown < people.length) return setOffset(start + shown);
    setOffset(0);
    onRedraw();
  };
  return <div className={styles.column}>
    <h3><MessageCircleHeart size={14} aria-hidden="true" />该联系了
      <button type="button" className={styles.redraw} onClick={next} disabled={redrawing}>
        <Shuffle size={12} aria-hidden="true" />换一批
      </button>
      <Link href="/insights">全部</Link>
    </h3>
    <ul className={styles.list}>{people.slice(start, start + shown).map((person) =>
      <Suggestion key={person.profile_id} person={person} />)}</ul>
  </div>;
}
