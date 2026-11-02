"use client";

import { scaleSqrt } from "d3-scale";
import { area, curveMonotoneX } from "d3-shape";
import { useRouter } from "next/navigation";
import { type KeyboardEvent, type PointerEvent, useLayoutEffect, useMemo, useRef, useState } from "react";

import { continuousMonths, monthLabel } from "../../lib/months";
import { motionAllowed } from "../../lib/zoom";
import styles from "./ribbon.module.css";

export type RibbonMonth = { month: string; sent: number; received: number; calls?: number; interactions?: number };
export type RibbonMarker = { month: string; kind: "date" | "event"; label: string };
export type RibbonData = { timezone: string; months: RibbonMonth[]; markers?: RibbonMarker[] };

const HEIGHT = 128;
const AXIS = 18;
const MID = (HEIGHT - AXIS) / 2;
const REACH = MID - 6;

// The draw-in plays once per profile per visit, not on every tab switch or refetch.
const drawn = new Set<string>();

/**
 * The relationship over its whole life: what I sent rises above the line, what they sent falls below it.
 * Calls tick the baseline, Moments interactions dot the top edge, dated milestones sit on the axis.
 * Hover or arrow keys show a month; click or Enter opens that month's messages.
 */
export function Ribbon({ profileId, data }: { profileId: string; data: RibbonData }) {
  const router = useRouter();
  const frame = useRef<HTMLDivElement>(null);
  const cover = useRef<SVGRectElement>(null);
  const [width, setWidth] = useState(0);
  const [active, setActive] = useState<number | null>(null);

  const months = useMemo(() => continuousMonths<RibbonMonth>(data.months, (month) => ({ month, sent: 0, received: 0 })), [data.months]);
  const markers = useMemo(() => {
    const index = new Map(months.map((row, position) => [row.month, position]));
    return (data.markers ?? []).flatMap((marker) => index.has(marker.month) ? [{ ...marker, index: index.get(marker.month)! }] : []);
  }, [data.markers, months]);
  const peak = Math.max(1, ...months.map((row) => Math.max(row.sent, row.received)));
  const peakIndex = months.findIndex((row) => row.sent + row.received === Math.max(...months.map((item) => item.sent + item.received)));
  const step = months.length > 1 ? width / (months.length - 1) : width;
  const x = (index: number) => months.length > 1 ? index * step : width / 2;
  const reach = scaleSqrt().domain([0, peak]).range([0, REACH]);

  const sentPath = area<RibbonMonth>().curve(curveMonotoneX).x((_, index) => x(index)).y0(MID).y1((row) => MID - reach(row.sent))(months) ?? "";
  const receivedPath = area<RibbonMonth>().curve(curveMonotoneX).x((_, index) => x(index)).y0(MID).y1((row) => MID + reach(row.received))(months) ?? "";
  const years = months.flatMap((row, index) => row.month.endsWith("-01") || (index === 0 && !months.slice(1, 5).some((item) => item.month.endsWith("-01")))
    ? [{ index, year: row.month.slice(0, 4) }] : []);
  const yearEvery = Math.max(1, Math.ceil(years.length / Math.max(1, Math.floor(width / 56))));

  useLayoutEffect(() => {
    const element = frame.current!;
    const observer = new ResizeObserver(([entry]) => setWidth(Math.round(entry.contentRect.width)));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  useLayoutEffect(() => {
    if (!width || drawn.has(profileId) || !cover.current) return;
    drawn.add(profileId);
    if (!motionAllowed()) return;
    cover.current.animate([{ transform: "translateX(0)", opacity: 1 }, { transform: `translateX(${width}px)`, opacity: 1 }], { duration: 600, easing: "cubic-bezier(.3, .6, .2, 1)" });
  }, [width, profileId]);

  const open = (index: number) => router.push(`/profiles/${profileId}?tab=messages&month=${months[index].month}&timezone=${encodeURIComponent(data.timezone)}`, { scroll: false });
  function point(event: PointerEvent<SVGSVGElement>) {
    const box = event.currentTarget.getBoundingClientRect();
    setActive(Math.max(0, Math.min(months.length - 1, Math.round((event.clientX - box.left) / (step || 1)))));
  }
  function key(event: KeyboardEvent<SVGSVGElement>) {
    const moves: Record<string, number> = { ArrowLeft: -1, ArrowRight: 1, PageUp: -12, PageDown: 12 };
    if (event.key in moves) {
      event.preventDefault();
      setActive((current) => Math.max(0, Math.min(months.length - 1, (current ?? months.length - 1) + moves[event.key])));
    } else if (event.key === "Home" || event.key === "End") {
      event.preventDefault();
      setActive(event.key === "Home" ? 0 : months.length - 1);
    } else if (event.key === "Enter" && active !== null) open(active);
  }

  if (!months.length) return null;
  const shown = active === null ? null : months[active];
  const tipLeft = active === null ? 0 : Math.max(70, Math.min(width - 70, x(active)));
  return <figure className={styles.ribbon} ref={frame}>
    <figcaption className="sr-only">{monthLabel(months[0].month)}至今的私聊：上方为我发送，下方为对方发送。可用左右方向键逐月查看，回车打开该月聊天。</figcaption>
    {width > 0 && <svg width={width} height={HEIGHT} viewBox={`0 0 ${width} ${HEIGHT}`} tabIndex={0} role="group"
      aria-label="关系丝带：每月私聊" onPointerMove={point} onPointerLeave={() => setActive(null)} onKeyDown={key}
      onBlur={() => setActive(null)} onClick={() => active !== null && open(active)}>
      {peakIndex >= 0 && <rect className={styles.peak} x={x(peakIndex) - Math.max(2, step / 2)} y={0} width={Math.max(4, step)} height={HEIGHT - AXIS} rx={2} />}
      <path className={styles.sent} d={sentPath} />
      <path className={styles.received} d={receivedPath} />
      <line className={styles.baseline} x1={0} x2={width} y1={MID} y2={MID} />
      {months.map((row, index) => (row.calls ?? 0) > 0 && <line key={`c${row.month}`} className={styles.call} x1={x(index)} x2={x(index)} y1={HEIGHT - AXIS - 8} y2={HEIGHT - AXIS} />)}
      {months.map((row, index) => (row.interactions ?? 0) > 0 && <circle key={`i${row.month}`} className={styles.interaction} cx={x(index)} cy={4} r={Math.min(3.5, 1.5 + Math.sqrt(row.interactions!) / 2)} />)}
      {markers.map((marker) => <path key={`${marker.kind}${marker.month}${marker.label}`} className={styles.marker} data-kind={marker.kind}
        d={`M${x(marker.index)} ${MID - 5} l5 5 -5 5 -5 -5z`}><title>{marker.label}</title></path>)}
      {years.filter((_, index) => index % yearEvery === 0).map(({ index, year }) => <text key={year} className={styles.year} x={x(index)} y={HEIGHT - 4}
        textAnchor={index === 0 ? "start" : "middle"}>{year}</text>)}
      {active !== null && <line className={styles.crosshair} x1={x(active)} x2={x(active)} y1={0} y2={HEIGHT - AXIS} />}
      <rect ref={cover} className={styles.cover} x={0} y={0} width={width} height={HEIGHT} />
    </svg>}
    {shown && <div className={styles.tip} style={{ left: tipLeft }} aria-live="polite">
      <strong>{monthLabel(shown.month)}</strong>
      <span><i data-series="sent" />我 {shown.sent.toLocaleString("zh-CN")}</span>
      <span><i data-series="received" />对方 {shown.received.toLocaleString("zh-CN")}</span>
      {(shown.calls ?? 0) > 0 && <span>通话 {shown.calls}</span>}
      {(shown.interactions ?? 0) > 0 && <span>朋友圈互动 {shown.interactions}</span>}
      {markers.filter((marker) => marker.month === shown.month).map((marker) => <span key={marker.label}>{marker.label}</span>)}
    </div>}
  </figure>;
}
