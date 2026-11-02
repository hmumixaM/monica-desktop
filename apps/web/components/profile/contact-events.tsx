"use client";

import { ArrowLeftRight, Phone } from "lucide-react";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import type { CallSummary, MoneySummary } from "../../lib/api-types";
import { useProfileTab } from "../../lib/queries";
import { LoadMore } from "../ui/load-more";
import { Failure, Faint, Loading, Muted, Panel, Section, Stat, formatDateTime } from "../ui/primitives";
import { SegmentedLinks } from "../ui/segmented";
import styles from "./profile.module.css";

type Summary = { calls: CallSummary; money: MoneySummary; snapshot_at?: string };
type Row = {
  account_id: string; external_id: string; occurred_at?: string;
  content: {
    media_type?: string; media_type_confidence?: string; direction?: string; status_label?: string; duration_seconds?: number | null; kind?: string;
    amount_minor?: number | null; currency?: string; counterparty?: string; initiator?: string; memo?: string; settled_at?: number; direction_source?: string;
  };
};

const PAGE = 30;
export const amount = (minor: number | null | undefined, currency?: string) => minor == null ? "金额未保留"
  : `${(minor / 100).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} ${currency || "币种未记录"}`;
const duration = (seconds: number) => `${Math.floor(seconds / 3600)}:${String(Math.floor(seconds % 3600 / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;

function useSummary(profileId: string) {
  return useProfileTab<Summary>(profileId, "contact-events-summary");
}

/** Left-column summary: two rows that open the matching sub-tab. Hidden when nothing was recorded. */
export function ContactEventSidebar({ profileId }: { profileId: string }) {
  const summary = useSummary(profileId);
  const data = summary.data;
  if (summary.isError) return <Section title="通话与收支"><Failure onRetry={() => void summary.refetch()}>记录读取失败。</Failure></Section>;
  if (!data || (!data.calls.total && !data.money.total)) return null;
  return <Section title="通话与收支" icon={<Phone size={15} aria-hidden="true" />}>
    <div className={styles.eventLinks}>
      {data.calls.total > 0 && <Link href={`/profiles/${profileId}?tab=contact-events&kind=call`}>
        <Phone size={15} aria-hidden="true" /><span>微信通话<small>{data.calls.voice} 语音 · {data.calls.video} 视频</small></span><b>{data.calls.total}</b>
      </Link>}
      {data.money.total > 0 && <Link href={`/profiles/${profileId}?tab=contact-events&kind=money`}>
        <ArrowLeftRight size={15} aria-hidden="true" /><span>金钱往来<small>{data.money.transfer} 转账 · {data.money.red_packet} 红包</small></span><b>{data.money.total}</b>
      </Link>}
    </div>
    {Object.entries(data.money.currencies).map(([currency, totals]) => <Faint key={currency}>收到 {amount(totals.in, currency)} · 支出 {amount(totals.out, currency)}</Faint>)}
  </Section>;
}

export function ContactEvents({ profileId, kind = "call" }: { profileId: string; kind?: string }) {
  const selected = kind === "money" ? "money" : "call";
  const summary = useSummary(profileId);
  const [rows, setRows] = useState<Row[]>([]);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const [more, setMore] = useState(false);
  const request = useRef<AbortController | null>(null);

  async function load(offset = 0) {
    if (offset && busy) return;
    request.current?.abort();
    const abort = new AbortController();
    request.current = abort;
    setBusy(true);
    setFailed(false);
    try {
      const response = await fetch(`/api/v1/profiles/${profileId}/contact-events?kind=${selected}&offset=${offset}`, { signal: abort.signal });
      if (!response.ok) throw new Error(String(response.status));
      const next: Row[] = await response.json();
      if (abort.signal.aborted) return;
      setRows((old) => offset ? [...old, ...next] : next);
      setMore(next.length === PAGE);
    } catch {
      if (!abort.signal.aborted) setFailed(true);
    } finally {
      if (!abort.signal.aborted) setBusy(false);
    }
  }
  useEffect(() => {
    setRows([]);
    setMore(false);
    void load();
    return () => request.current?.abort();
  }, [profileId, selected]);

  const calls = summary.data?.calls;
  const money = summary.data?.money;
  const isCall = selected === "call";
  return <Panel className={styles.tabPanel}>
    <header className={styles.tabHeader}>
      <h2>{isCall ? "微信音视频通话" : "微信金钱往来"}</h2>
      <SegmentedLinks label="记录类型" size="sm" options={[
        { href: `/profiles/${profileId}?tab=contact-events&kind=call`, label: "通话", active: isCall },
        { href: `/profiles/${profileId}?tab=contact-events&kind=money`, label: "收支", active: !isCall },
      ]} />
    </header>
    {summary.isError && <Failure onRetry={() => void summary.refetch()} retryLabel="重试统计">统计读取失败。</Failure>}
    {isCall && calls && <>
      <div className={styles.statRow}>
        <Stat value={calls.voice} label="语音通话" /><Stat value={calls.video} label="视频通话" />
        <Stat value={calls.completed} label="已接通 / 结束" /><Stat value={duration(calls.duration_seconds)} label={`${calls.known_duration} 次已知时长`} />
      </div>
      <Muted>收到 {calls.incoming} 次 · 呼出 {calls.outgoing} 次{calls.inferred_type > 0 && ` · ${calls.inferred_type} 次媒体类型根据记录推断`}</Muted>
    </>}
    {!isCall && money && <>
      <div className={styles.statRow}>
        <Stat value={money.total} label="条往来" /><Stat value={money.transfer} label={`转账 · ${money.red_packet} 个红包`} />
        {Object.entries(money.currencies).map(([currency, totals]) => <Stat key={currency} size="sm" value={amount(totals.in, currency)} label={`收到 · 支出 ${amount(totals.out, currency)}`} />)}
      </div>
      <Muted>{money.unknown_amount} 条未保留金额 · {money.unknown_direction} 条方向未知。按已知金额分别统计，不折算为欠款余额。</Muted>
    </>}
    {summary.data?.snapshot_at && <Faint>归档于 {formatDateTime(summary.data.snapshot_at)}</Faint>}
    {rows.length > 0 && <div className={styles.tableScroll}><table className={styles.table}>
      <thead><tr>
        <th>时间</th><th>类型</th><th>方向</th><th>{isCall ? "时长" : "金额"}</th><th>状态</th>
        {!isCall && <><th>对方 / 来源</th><th>到账时间</th><th>备注</th></>}
      </tr></thead>
      <tbody>{rows.map((row) => {
        const item = row.content;
        const inferred = !isCall && item.direction && ["initiator", "lifecycle"].includes(item.direction_source || "");
        return <tr key={`${row.account_id}:${row.external_id}`}>
          <td>{formatDateTime(row.occurred_at)}</td>
          <td>{isCall ? `${item.media_type === "video" ? "视频" : "语音"}${item.media_type_confidence !== "high" ? "（推断）" : ""}` : item.kind === "red_packet" ? "红包" : "转账"}</td>
          <td>{item.direction === "in" ? (isCall ? "收到" : "收入") : item.direction === "out" ? (isCall ? "呼出" : "支出") : "未知"}{inferred && <small> 推断</small>}</td>
          <td>{isCall ? (item.duration_seconds == null ? "未保留" : duration(item.duration_seconds)) : amount(item.amount_minor, item.currency)}</td>
          <td>{item.status_label || "未知"}</td>
          {!isCall && <><td>{item.counterparty || "—"}</td><td>{item.settled_at ? formatDateTime(item.settled_at) : "—"}</td><td>{item.memo || "—"}</td></>}
        </tr>;
      })}</tbody>
    </table></div>}
    {failed && <Failure onRetry={() => void load(rows.length)}>记录读取失败。</Failure>}
    {busy && <Loading>正在读取记录…</Loading>}
    {!busy && !failed && !rows.length && <Muted>尚未收录此类记录。</Muted>}
    {more && <LoadMore cursor={rows.at(-1)?.external_id || ""} busy={busy} error={failed} onLoad={() => void load(rows.length)} label="加载更早的记录" />}
  </Panel>;
}
