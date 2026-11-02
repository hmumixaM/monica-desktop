"use client";

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { type KeyboardEvent, useEffect, useMemo, useRef, useState } from "react";

import { fetchJson } from "../../lib/queries";
import { Select } from "../ui/field";
import { Avatar, Faint, Failure, formatNumber, Skeleton } from "../ui/primitives";
import styles from "./calendar.module.css";

type CalendarYear = { year: number; years: number[]; days: { date: string; people: number }[]; max: number };
type CalendarDay = { date: string; people: { profile_id: string; name: string; avatar_media_id?: string | null; messages: number; calls: number }[] };

const WEEKDAYS = ["一", "", "三", "", "五", "", "日"];
const DAY_MS = 86_400_000;

const iso = (date: Date) => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
const dayLabel = (value: string) => {
  const [, month, day] = value.split("-").map(Number);
  return `${month} 月 ${day} 日`;
};

/** Cells from the Monday on or before 1 January to 31 December, weeks as columns. */
function cells(year: number) {
  const first = new Date(year, 0, 1);
  const start = new Date(year, 0, 1 - ((first.getDay() + 6) % 7));
  const end = new Date(year, 11, 31);
  const days: { date: string; inYear: boolean; week: number; month: number }[] = [];
  for (let at = start, index = 0; at <= end; at = new Date(at.getFullYear(), at.getMonth(), at.getDate() + 1), index += 1) {
    days.push({ date: iso(at), inYear: at.getFullYear() === year, week: Math.floor(index / 7), month: at.getMonth() });
  }
  return days;
}

/** Quartile cut points over the days you talked to anyone, so one huge day does not wash out the rest. */
function levels(values: number[]) {
  const sorted = values.filter(Boolean).sort((a, b) => a - b);
  const at = (share: number) => sorted[Math.min(sorted.length - 1, Math.floor(share * sorted.length))] ?? 1;
  const cuts = [at(0.25), at(0.5), at(0.8)];
  return (value: number) => !value ? 0 : value <= cuts[0] ? 1 : value <= cuts[1] ? 2 : value <= cuts[2] ? 3 : 4;
}

/** 联系日历: how many people you were in touch with each day of a year; a day lists who they were. */
export function ContactCalendar() {
  const [year, setYear] = useState(() => new Date().getFullYear());
  const [day, setDay] = useState<string | null>(null);
  const grid = useRef<HTMLDivElement>(null);
  const calendar = useQuery({
    queryKey: ["calendar", year],
    queryFn: ({ signal }) => fetchJson<CalendarYear>(`/api/v1/insights/calendar?year=${year}`, { signal }),
    placeholderData: keepPreviousData, staleTime: 10 * 60_000,
  });
  const detail = useQuery({
    queryKey: ["calendar", "day", day],
    queryFn: ({ signal }) => fetchJson<CalendarDay>(`/api/v1/insights/calendar/day?day=${day}`, { signal }),
    enabled: !!day,
  });
  const data = calendar.data;
  const counts = useMemo(() => new Map(data?.days.map((entry) => [String(entry.date), entry.people])), [data]);
  const level = useMemo(() => levels([...counts.values()]), [counts]);
  const days = useMemo(() => cells(year), [year]);
  const today = iso(new Date());
  const focusable = day ?? (days.some((cell) => cell.date === today) ? today : days[days.length - 1].date);
  const months = useMemo(() => days.filter((cell, index) => cell.inYear && (index === 0 || days[index - 1].month !== cell.month || !days[index - 1].inYear))
    .map((cell) => ({ month: cell.month, week: cell.week })), [days]);

  useEffect(() => {
    const element = grid.current;
    if (element) element.scrollLeft = year === new Date().getFullYear() ? element.scrollWidth : 0;
  }, [year, data]);

  if (calendar.isError && !data) return <Failure onRetry={() => void calendar.refetch()}>暂时无法读取联系日历。</Failure>;
  if (!data) return <Skeleton height={180} />;

  const active = data.days.filter((entry) => entry.people > 0);
  const total = active.reduce((sum, entry) => sum + entry.people, 0);
  const move = (event: KeyboardEvent) => {
    const step = { ArrowLeft: -7, ArrowRight: 7, ArrowUp: -1, ArrowDown: 1 }[event.key];
    if (!step) return;
    event.preventDefault();
    const [y, m, d] = focusable.split("-").map(Number);
    const next = iso(new Date(new Date(y, m - 1, d).getTime() + step * DAY_MS + DAY_MS / 2));
    if (!next.startsWith(String(year)) || next > today) return;
    setDay(next);
    grid.current?.querySelector<HTMLButtonElement>(`[data-date="${next}"]`)?.focus();
  };

  return <div className={styles.calendar}>
    <div className={styles.head}>
      <p className={styles.summary}>
        <span><b>{active.length}</b> 天有联系</span>
        <span>平均每天 <b>{active.length ? (total / active.length).toFixed(1) : 0}</b> 人</span>
        <span>最多一天 <b>{data.max}</b> 人</span>
      </p>
      {data.years.length > 1 && <Select aria-label="年份" value={year} onChange={(event) => { setYear(Number(event.target.value)); setDay(null); }}>
        {[...data.years].reverse().map((value) => <option key={value} value={value}>{value}</option>)}
      </Select>}
    </div>
    <div className={styles.scroller} ref={grid}>
      <div className={styles.frame}>
        <div className={styles.months} aria-hidden="true">
          {months.map((entry) => <span key={entry.month} style={{ gridColumnStart: entry.week + 1 }}>{entry.month + 1}月</span>)}
        </div>
        <div className={styles.weekdays} aria-hidden="true">{WEEKDAYS.map((label, index) => <span key={index}>{label}</span>)}</div>
        <div className={styles.grid} role="grid" aria-label={`${year} 年每天联系的人数`} onKeyDown={move}>
          {days.map((cell) => {
            const value = counts.get(cell.date) ?? 0;
            const future = cell.date > today;
            if (!cell.inYear || future) return <span key={cell.date} className={styles.cell} data-out="" />;
            return <button key={cell.date} type="button" role="gridcell" className={styles.cell} data-date={cell.date} data-level={level(value)}
              aria-selected={day === cell.date} tabIndex={cell.date === focusable ? 0 : -1}
              aria-label={`${dayLabel(cell.date)}，${value} 人`} title={`${dayLabel(cell.date)} · ${value} 人`}
              onClick={() => setDay(day === cell.date ? null : cell.date)} />;
          })}
        </div>
      </div>
    </div>
    <div className={styles.scale} aria-hidden="true"><span>少</span>{[0, 1, 2, 3, 4].map((value) => <i key={value} data-level={value} />)}<span>多</span></div>

    {day && <div className={styles.day} aria-live="polite">
      <h3>{dayLabel(day)}{detail.data && <small>{detail.data.people.length} 人</small>}</h3>
      {detail.isError ? <Failure onRetry={() => void detail.refetch()}>读取失败。</Failure>
        : !detail.data ? <Faint>正在读取…</Faint>
          : !detail.data.people.length ? <Faint>这天没有私聊或通话。</Faint>
            : <ul>{detail.data.people.map((person) => <li key={person.profile_id}>
              <Link href={`/profiles/${person.profile_id}?tab=messages`}>
                <Avatar name={person.name} mediaId={person.avatar_media_id} size={28} round />
                <span className={styles.dayName}>{person.name}</span>
                <small>{[person.messages && `${formatNumber(person.messages)} 条`, person.calls && `${person.calls} 次通话`].filter(Boolean).join(" · ")}</small>
              </Link>
            </li>)}</ul>}
    </div>}
  </div>;
}
