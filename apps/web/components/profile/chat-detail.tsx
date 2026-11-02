"use client";

import { CalendarRange } from "lucide-react";

import { monthLabel } from "../../lib/months";
import { useProfileTab } from "../../lib/queries";
import { MonthlyBars } from "../charts/monthly-bars";
import { Conversation } from "../messages/conversation";
import { Failure, Faint, Panel, Skeleton, Stat, formatNumber } from "../ui/primitives";
import styles from "./profile.module.css";

type ChatMonths = {
  months: { month: string; total: number; sent: number; received: number }[]; total: number; sent: number; received: number;
  first?: string; last?: string; snapshot_at?: string; active_months: number; peak?: { month: string; total: number }; timezone: string;
};

function MonthlyDetail({ profileId }: { profileId: string }) {
  const summary = useProfileTab<ChatMonths>(profileId, "chat-months");
  const data = summary.data;
  if (summary.isError) return <Panel><Failure onRetry={() => void summary.refetch()}>月度统计读取失败。</Failure></Panel>;
  if (!data) return <Skeleton height={220} />;
  if (!data.months.length) return null;
  const date = (value?: string) => value ? new Date(value).toLocaleDateString("zh-CN", { timeZone: data.timezone }) : "未收录";
  return <Panel className={styles.monthly} label="月度明细">
    <header className={styles.blockHeader}>
      <h2><CalendarRange size={15} aria-hidden="true" />月度明细</h2>
      {data.snapshot_at && <small>统计截至 {date(data.snapshot_at)}</small>}
    </header>
    <div className={styles.statRow}>
      <Stat value={formatNumber(data.total)} label="全部私聊" />
      <Stat value={formatNumber(data.sent)} label="我发送" />
      <Stat value={formatNumber(data.received)} label="对方发送" />
      {data.peak && <Stat highlight size="sm" value={monthLabel(data.peak.month)} label={`高峰月 · ${formatNumber(data.peak.total)} 条`} />}
      <Stat size="sm" value={data.active_months} label="有消息的月份" />
      <Stat size="sm" value={date(data.first)} label="首次联系" />
    </div>
    <MonthlyBars rows={data.months} label="每月私聊" stacked
      href={(month) => `/profiles/${profileId}?tab=messages&month=${month}&timezone=${encodeURIComponent(data.timezone)}`} />
    <details className={styles.more}>
      <summary>每月精确数据</summary>
      <div className={styles.tableScroll}><table className={styles.table}>
        <thead><tr><th>月份</th><th>我发送</th><th>对方发送</th><th>合计</th></tr></thead>
        <tbody>{[...data.months].reverse().map((row) => <tr key={row.month}>
          <th scope="row">{row.month}</th><td>{formatNumber(row.sent)}</td><td>{formatNumber(row.received)}</td><td>{formatNumber(row.total)}</td>
        </tr>)}</tbody>
      </table></div>
    </details>
  </Panel>;
}

/** The 聊天 tab: the month-by-month detail sits above the conversation it filters. */
export function MessagesTab({ profileId, person }: { profileId: string; person: boolean }) {
  return <div className={styles.messagesTab} data-tab-panel="messages">
    {person && <MonthlyDetail profileId={profileId} />}
    <Panel className={styles.thread}><Conversation profileId={profileId} counterpartId={person ? profileId : undefined} /></Panel>
  </div>;
}
