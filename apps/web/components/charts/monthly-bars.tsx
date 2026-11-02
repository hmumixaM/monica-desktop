"use client";

import { ChevronLeft, ChevronRight } from "lucide-react";
import Link from "next/link";
import { type CSSProperties, useEffect, useMemo, useRef } from "react";

import { continuousMonths } from "../../lib/months";
import { motionAllowed } from "../../lib/zoom";
import { Button, IconButton } from "../ui/button";
import { Faint } from "../ui/primitives";
import styles from "./charts.module.css";

export type MonthRow = { month: string; total: number; sent?: number; received?: number };

/**
 * Every month as a bar, zero months included, scrolling sideways. Opens near the latest activity;
 * each bar links to that month. With `stacked`, bars split into what I sent and what they sent.
 */
export function MonthlyBars({ rows, label, href, stacked = false }: { rows: MonthRow[]; label: string; href: (month: string) => string; stacked?: boolean }) {
  const scroller = useRef<HTMLDivElement>(null);
  const months = useMemo(() => continuousMonths(rows, (month) => ({ month, total: 0, sent: 0, received: 0 })), [rows]);
  const peak = Math.max(1, ...months.map((row) => row.total));
  const lastActive = months.findLast((row) => row.total > 0)?.month;
  const behavior = (smooth: boolean): ScrollBehavior => smooth && motionAllowed() ? "smooth" : "instant";

  function goTo(month: string | undefined, smooth = true) {
    const viewport = scroller.current;
    const target = month ? viewport?.querySelector<HTMLElement>(`[data-month="${month}"]`) : null;
    if (viewport && target) viewport.scrollTo({ left: target.offsetLeft - viewport.clientWidth * 0.7, behavior: behavior(smooth) });
  }
  useEffect(() => goTo(lastActive ?? months.at(-1)?.month, false), [lastActive, months.length]);
  const page = (direction: number) => scroller.current?.scrollBy({ left: direction * scroller.current.clientWidth * 0.8, behavior: behavior(true) });

  if (!months.length) return <Faint>尚无带日期的记录</Faint>;
  return <div className={styles.bars}>
    <div className={styles.barsNav}>
      <span>{months[0].month} — {months.at(-1)!.month}
        {stacked && <span className={styles.keys}><i data-series="sent" />我发送<i data-series="received" />对方发送</span>}</span>
      <div>
        <IconButton size="sm" label={`${label}：更早`} onClick={() => page(-1)}><ChevronLeft size={15} /></IconButton>
        <Button size="sm" variant="ghost" onClick={() => goTo(lastActive)}>最近记录</Button>
        <IconButton size="sm" label={`${label}：更近`} onClick={() => page(1)}><ChevronRight size={15} /></IconButton>
      </div>
    </div>
    <div className={styles.barsTrack} ref={scroller} role="region" aria-label={`${label}，按月排列，可横向滑动`} tabIndex={0}>
      {months.map((row) => <Link key={row.month} href={href(row.month)} scroll={false} data-month={row.month} data-empty={row.total ? undefined : ""}
        aria-label={`${row.month}，${row.total} 条${stacked ? `，我发送 ${row.sent ?? 0}，对方发送 ${row.received ?? 0}` : ""}`}
        style={{ "--level": row.total / peak } as CSSProperties}>
        <b>{row.total ? row.total.toLocaleString("zh-CN") : ""}</b>
        <i data-peak={row.total === peak ? "" : undefined}>
          {stacked && <><span data-series="sent" style={{ flex: row.sent ?? 0 }} /><span data-series="received" style={{ flex: row.received ?? 0 }} /></>}
        </i>
        <small data-year={row.month.endsWith("-01") ? "" : undefined}>{row.month.endsWith("-01") ? row.month.slice(0, 4) : Number(row.month.slice(5))}</small>
      </Link>)}
    </div>
  </div>;
}
