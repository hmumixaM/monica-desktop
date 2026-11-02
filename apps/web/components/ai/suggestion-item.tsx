"use client";

import Link from "next/link";
import { type FormEvent, useState } from "react";

import { type Evidence, type ProfileSuggestion, type SuggestionDecision, useDecideSuggestion } from "../../lib/ai";
import { ApiError } from "../../lib/queries";
import { Button } from "../ui/button";
import { Input } from "../ui/field";
import { useToast } from "../ui/toast";
import styles from "./suggestions.module.css";

const FAILURE: Record<number, string> = {
  409: "已经有生日了，先在纪念日里修改",
  422: "格式不对：生日写 MM-DD 或 YYYY-MM-DD，聚会日期要在近期",
};

/** Bare `YYYY-MM-DD` is a calendar day, not UTC midnight, so it must not shift with the time zone. */
function day(at: string): string {
  return new Date(at.length === 10 ? `${at}T00:00` : at).toLocaleDateString("zh-CN", { year: "numeric", month: "numeric", day: "numeric" });
}

function evidenceHref(item: Evidence): string {
  if (item.kind === "post") return `/timeline?event=${item.id}`;
  if (item.kind === "profile") return `/profiles/${item.id}`;
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  return `/messages?${new URLSearchParams({ conversation: item.conversation_id ?? "", month: item.at.slice(0, 7), timezone })}`;
}

function evidenceLabel(item: Evidence): string {
  if (item.kind === "post") return `动态 · ${day(item.at)}`;
  if (item.kind === "profile") return `资料 / 笔记 · ${day(item.at)}`;
  if (item.kind === "group") return `群「${item.group}」· ${day(item.at)}`;
  return `微信私聊 · ${day(item.at)}`;
}

/** One pending suggestion: nothing is written until it is accepted, optionally after editing the value. */
export function SuggestionItem({ profileId, item }: { profileId: string; item: ProfileSuggestion }) {
  const decide = useDecideSuggestion();
  const toast = useToast();
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(item.value);
  const [date, setDate] = useState(item.date ?? "");

  // The row unmounts as soon as it is decided, which drops per-call mutate callbacks; the promise still settles.
  const send = (decision: SuggestionDecision, done: string, undo?: () => void) => decide
    .mutateAsync({ profileId, fingerprint: item.fingerprint, decision })
    .then(
      () => toast.show({ message: done, ...(undo && { actionLabel: "撤销", onAction: undo }) }),
      (error) => toast.show({ message: (error instanceof ApiError && FAILURE[error.status]) || "没能保存，请重试" }),
    );
  const accept = (event?: FormEvent) => {
    event?.preventDefault();
    const edited = editing ? { value: value.trim(), ...(item.kind === "meetup" && date && { date }) } : {};
    send({ action: "accept", ...edited }, `已写入${item.label}`);
  };
  const ignore = () => send({ action: "ignore" }, `已忽略${item.label}建议`,
    () => decide.mutate({ profileId, fingerprint: item.fingerprint, decision: null }));

  return <li className={styles.item}>
    {editing
      ? <form className={styles.edit} onSubmit={accept}>
        <Input aria-label={item.label} value={value} onChange={(event) => setValue(event.target.value)} autoFocus />
        {item.kind === "meetup" && <Input type="date" aria-label="日期" value={date} onChange={(event) => setDate(event.target.value)} />}
      </form>
      : <div className={styles.head}>
        <span className={styles.label}>{item.label}</span>
        <strong>{item.value}</strong>
        {item.date && <time dateTime={item.date}>{day(item.date)}</time>}
        {item.confidence === "medium" && <span className={styles.maybe}>可能</span>}
      </div>}
    {item.quote && <blockquote className={styles.quote}>{item.quote}</blockquote>}
    <div className={styles.foot}>
      <p className={styles.sources}>
        {item.evidence.map((source, index) => <Link key={`${source.kind}:${source.id}:${index}`} href={evidenceHref(source)} title={source.text}>{evidenceLabel(source)}</Link>)}
      </p>
      <div className={styles.actions}>
        <Button size="sm" variant="secondary" onClick={() => accept()} disabled={decide.isPending || (editing && !value.trim())}>
          {editing ? "保存" : "接受"}
        </Button>
        {editing
          ? <Button size="sm" variant="ghost" onClick={() => { setEditing(false); setValue(item.value); setDate(item.date ?? ""); }}>取消</Button>
          : <Button size="sm" variant="ghost" onClick={() => setEditing(true)}>修改</Button>}
        {!editing && <Button size="sm" variant="ghost" onClick={ignore} disabled={decide.isPending}>忽略</Button>}
      </div>
    </div>
  </li>;
}
