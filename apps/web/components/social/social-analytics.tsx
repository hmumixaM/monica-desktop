"use client";

import { useQuery } from "@tanstack/react-query";
import { BarChart3 } from "lucide-react";
import Link from "next/link";
import { type CSSProperties, useMemo, useState } from "react";

import { fetchJson } from "../../lib/queries";
import { MonthlyBars } from "../charts/monthly-bars";
import { Failure, Faint, Panel, Skeleton, SourceMark, providerLabel } from "../ui/primitives";
import { Segmented } from "../ui/segmented";
import styles from "./social.module.css";

type Row = { name: string; count?: number; value?: number; profile_id: string; posts?: number };
type Stats = {
  people: number; authors: number; posts: number; likes: number; comments: number; local_photo_posts: number; first?: string; latest?: string;
  groups?: number; indexed_messages?: number; conversations?: number; monthly: { month: string; count: number }[]; leaders: Row[];
  charts: { title: string; rows: Row[]; denominator: number; filter?: string }[];
  chat?: { contacts: number; messages: number; sent: number; received: number; leaders: Row[]; group_leaders: Row[] };
};
type Metrics = { providers: Record<string, Stats>; as_of: string; scope: string };
type Source = "wechat" | "instagram" | "linkedin";
const SOURCES: Source[] = ["wechat", "instagram", "linkedin"];
const n = (value?: number) => value == null ? "—" : value.toLocaleString("zh-CN");

function useSocialMetrics(profileId?: string) {
  return useQuery({
    queryKey: profileId ? ["profile", profileId, "social", {}] : ["social"],
    queryFn: () => fetchJson<Metrics>(`/api/v1/metrics/social${profileId ? `?profile_id=${profileId}` : ""}`),
  });
}

function Ranking({ title, rows, caption, filter, messages }: { title: string; rows: Row[]; caption: string; filter?: string; messages?: boolean }) {
  if (!rows.length) return null;
  const max = Math.max(1, ...rows.map((row) => row.value ?? row.count ?? 0));
  return <section className={styles.ranking}>
    <header><h3>{title}</h3><small>{caption}</small></header>
    <ol>{rows.map((row, index) => {
      const value = row.value ?? row.count ?? 0;
      return <li key={`${row.profile_id}-${index}`} style={{ "--share": value / max } as CSSProperties}>
        <Link href={filter ? `/profiles?${filter}=${encodeURIComponent(row.name)}` : `/profiles/${row.profile_id}${messages ? "?tab=messages" : ""}`}>
          <span>{row.name}</span><b>{n(value)}</b>
        </Link>
      </li>;
    })}</ol>
  </section>;
}

/**
 * Posts, likes and comments per source with monthly activity and rankings. On a profile it describes that
 * person; without one it describes everyone. Absent on a profile with nothing recorded.
 */
export function SocialAnalytics({ profileId }: { profileId?: string }) {
  const metrics = useSocialMetrics(profileId);
  const available = useMemo(() => SOURCES.filter((source) => {
    const stats = metrics.data?.providers[source];
    return stats && (stats.posts > 0 || stats.people > 0 || !!stats.chat?.messages);
  }), [metrics.data]);
  const [chosen, setChosen] = useState<Source | null>(null);
  const source = chosen && available.includes(chosen) ? chosen : available[0];
  const stats = source ? metrics.data?.providers[source] : undefined;
  const months = useMemo(() => stats?.monthly.map((row) => ({ month: row.month, total: row.count })) ?? [], [stats]);
  const timeline = (month?: string) => {
    const params = new URLSearchParams({ provider: source ?? "" });
    if (month) params.set("month", month);
    return profileId ? `/profiles/${profileId}?tab=timeline&${params}` : `/timeline?${params}`;
  };

  if (metrics.isError) return <Panel><Failure onRetry={() => void metrics.refetch()}>暂时无法读取统计。</Failure></Panel>;
  if (!metrics.data) return <Skeleton height={260} />;
  if (!stats) return profileId ? null : <Panel><Faint>尚无可统计的社交记录。</Faint></Panel>;

  const totals: [string, number | undefined][] = [
    [profileId ? "发布动态" : "来源人物", profileId ? stats.posts : stats.people], [profileId ? "动态作者" : "发布过动态", stats.authors],
    ["已收录动态", stats.posts], ["收到点赞", stats.likes], ["收到评论", stats.comments], ["有本地媒体", stats.local_photo_posts],
  ];
  return <Panel className={styles.analytics} label={profileId ? "互动" : "社交统计"}>
    <header className={styles.header}>
      <h2><BarChart3 size={15} aria-hidden="true" />{profileId ? "互动" : "社交统计"}</h2>
      {available.length > 1 && <Segmented size="sm" label="统计平台" value={source!} onChange={setChosen}
        options={available.map((value) => ({ value, label: <><SourceMark provider={value} size={9} decorative />{providerLabel(value)}</> }))} />}
    </header>
    <div className={styles.totals}>{totals.filter(([, value]) => value != null).map(([label, value]) =>
      <Link key={label} href={timeline()}><b>{n(value)}</b><span>{label}</span></Link>)}</div>
    <Faint>{stats.first?.slice(0, 10) || "日期未收录"} 至 {stats.latest?.slice(0, 10) || "日期未收录"} · {stats.local_photo_posts}/{stats.posts} 条有本地媒体</Faint>
    {months.length > 0 && <section className={styles.months}><h3>每月动态</h3><MonthlyBars key={source} rows={months} label="每月动态" href={timeline} /></section>}
    <div className={styles.rankings}>
      <Ranking title={profileId ? "动态互动" : "收到互动最多的人"} rows={stats.leaders} caption="已收录帖子的赞与评论" />
      {stats.charts.map((chart) => <Ranking key={chart.title} title={chart.title} rows={chart.rows} filter={chart.filter}
        caption={chart.filter ? `不同联系人 · ${chart.denominator} 位 LinkedIn 人物` : chart.title.includes("活跃") ? `${chart.denominator} 份私聊归档摘要` : "采集时的主页计数"} />)}
      {source === "wechat" && stats.chat && <>
        <Ranking title="私聊消息" rows={stats.chat.leaders} caption="Chatlog 归档摘要，点击打开聊天" messages />
        <Ranking title="群聊发言" rows={stats.chat.group_leaders} caption={profileId ? "这个人在各群已导入的发言" : "Chatlog 归档摘要"} />
      </>}
    </div>
    {source === "wechat" && stats.chat && <p className={styles.chatNumbers}>
      <span><b>{n(stats.chat.contacts)}</b> 有私聊记录</span>
      <span><b>{n(stats.chat.messages)}</b> 归档私聊消息</span>
      {!profileId && <>
        <Link href="/profiles?type=group"><b>{n(stats.groups)}</b> 群组</Link>
        <Link href="/messages"><b>{n(stats.conversations)}</b> 会话</Link>
        <Link href="/messages"><b>{n(stats.indexed_messages)}</b> 已进入搜索的消息</Link>
      </>}
    </p>}
  </Panel>;
}
