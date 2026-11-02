"use client";

import { RefreshCw, Sparkles } from "lucide-react";
import Link from "next/link";

import { type PersonWeeklyUpdate, type WeeklySource, useProfileAi, useRefreshProfileAi } from "../../lib/ai";
import { weekRange } from "../digest/week";
import { IconButton } from "../ui/button";
import { relativeDays, Section } from "../ui/primitives";
import { SuggestionItem } from "./suggestion-item";
import styles from "./suggestions.module.css";

function sourceHref(source: WeeklySource, profileId: string) {
  if (source.kind === "post" || source.kind === "story") return `/timeline?event=${source.id}`;
  if (source.kind === "profile") return `/profiles/${profileId}`;
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  return `/messages?${new URLSearchParams({ conversation: source.conversation_id ?? "", month: source.at?.slice(0, 7) ?? "", timezone })}`;
}

export function Week({ item, profileId, showWeek = true }: { item: PersonWeeklyUpdate; profileId: string; showWeek?: boolean }) {
  return <article className={styles.week}>
    {showWeek && <h3>{weekRange(item.week_start)}</h3>}
    <p>{item.summary}</p>
    {item.sources.length > 0 && <div className={styles.weekSources} aria-label="近况来源">
      {item.sources.map((source, index) => <Link key={`${source.kind}:${source.id}:${index}`} href={sourceHref(source, profileId)} title={source.quote}>
        {source.discovered ? "新发现的历史资料" : source.kind === "direct" ? "私聊" : source.kind === "group" ? "群聊" : source.kind === "profile" ? "资料" : "动态"}
        {source.at ? ` · ${source.at.slice(5, 10)}` : ""}
      </Link>)}
    </div>}
  </article>;
}

/** A person's long-term Memory, latest weekly note, and pending profile suggestions. */
export function ProfileAi({ profileId }: { profileId: string }) {
  const ai = useProfileAi(profileId);
  const refresh = useRefreshProfileAi(profileId);
  const data = ai.data;
  if (!data?.enabled || (!data.memory && !data.weekly_updates?.length && !data.suggestions?.length)) return null;
  const busy = data.queued || refresh.isPending;
  const suggestions = data.suggestions ?? [];
  const weeks = data.weekly_updates ?? [];
  const facts = data.memory?.facts ?? [];

  return <Section panel title="人物 Memory" icon={<Sparkles size={15} aria-hidden="true" />}
    action={<IconButton size="sm" label={busy ? "正在重新生成" : "重新生成"} disabled={busy} onClick={() => refresh.mutate()}>
      <RefreshCw size={15} aria-hidden="true" />
    </IconButton>}>
    <div className={styles.overview}>
      {data.memory && <>
        <p className={styles.summary}>{data.memory.summary}</p>
        {facts.length > 0 && <ul className={styles.memoryFacts}>{facts.slice(0, 5).map((fact, index) =>
          <li key={`${fact.label}:${index}`}><b>{fact.label}</b><span>{fact.value}</span></li>)}</ul>}
        {facts.length > 5 && <details className={styles.memoryMore}><summary>查看更多资料</summary><ul className={styles.memoryFacts}>
          {facts.slice(5).map((fact, index) => <li key={`${fact.label}:${index}`}><b>{fact.label}</b><span>{fact.value}</span></li>)}
        </ul></details>}
        {data.memory.gaps.length > 0 && <p className={styles.gaps}>还不知道：{data.memory.gaps.join("、")}</p>}
      </>}
      {weeks.length > 0 && <div className={styles.weekly}>
        <h3 className={styles.subhead}>每周近况</h3>
        <Week item={weeks[0]} profileId={profileId} />
        {weeks.length > 1 && <details className={styles.memoryMore}><summary>查看以前的近况</summary>
          {weeks.slice(1).map((item) => <Week key={item.week_start} item={item} profileId={profileId} />)}
        </details>}
      </div>}
      {suggestions.length > 0 && <>
        <h3 className={styles.subhead}>可以补充<b>{suggestions.length}</b><Link href="/suggestions">全部建议</Link></h3>
        <ul className={styles.list}>{suggestions.map((item) => <SuggestionItem key={item.fingerprint} profileId={profileId} item={item} />)}</ul>
      </>}
      <p className={styles.status} role="status">
        {busy ? "正在重新生成…" : data.memory && `${relativeDays(data.memory.generated_at)}更新 · 基于已存资料、动态和聊天`}
      </p>
    </div>
  </Section>;
}
