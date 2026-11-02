"use client";

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { Pause, Play, X } from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import { type Atlas, type AtlasPoint, monthIndexes, monthLabel } from "../../lib/footprint";
import { fetchJson } from "../../lib/queries";
import { useResolvedTheme } from "../../lib/theme";
import { Button, IconButton } from "../ui/button";
import { Select, SearchInput } from "../ui/field";
import { Failure, Faint, formatNumber, Skeleton, SourceMark } from "../ui/primitives";
import { FootprintWorld } from "./footprint-world";
import styles from "./footprint.module.css";

const AREA_PLACES = 12;
const AREA_PEOPLE = 40;
const AREA_EVENTS = 20;
const STEP_MS = 450;

function useAtlas(provider: string, year: string, kind: string) {
  const params = new URLSearchParams({ kind });
  if (provider) params.set("provider", provider);
  if (year) params.set("year", year);
  return useQuery({
    queryKey: ["atlas", provider, year, kind],
    queryFn: ({ signal }) => fetchJson<Atlas>(`/api/v1/metrics/locations?${params}`, { signal }),
    placeholderData: keepPreviousData, staleTime: 10 * 60_000,
  });
}

/** 足迹地图: where everyone lives and has been, with a month-by-month replay of dated footprints. */
export function FootprintView() {
  const theme = useResolvedTheme();
  const [provider, setProvider] = useState("");
  const [year, setYear] = useState("");
  const [kind, setKind] = useState("all");
  const [query, setQuery] = useState("");
  const [results, setResults] = useState(false);
  const [selected, setSelected] = useState<AtlasPoint | null>(null);
  const [areaPoint, setAreaPoint] = useState<AtlasPoint | null>(null);
  const [month, setMonth] = useState<number | null>(null);
  const [playing, setPlaying] = useState(false);
  const atlas = useAtlas(provider, year, kind);
  const data = atlas.data;
  const months = data?.months ?? [];
  const indexes = useMemo(() => data ? monthIndexes(data.points, data.months) : [], [data]);
  const playback = useMemo(() => month == null ? null : { indexes, month }, [indexes, month]);
  const matches = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    if (!needle || !data) return [];
    return data.points.filter((point) => `${point.label} ${point.people.map((person) => person.name).join(" ")}`.toLocaleLowerCase().includes(needle));
  }, [data, query]);
  const now = useMemo(() => {
    if (!data || month == null) return [];
    const key = data.months[month];
    return data.points.filter((point) => point.months?.[key]).sort((a, b) => (b.months![key] - a.months![key]));
  }, [data, month]);

  useEffect(() => {
    setSelected(null);
    setAreaPoint(null);
    setMonth(null);
    setPlaying(false);
  }, [provider, year, kind]);

  useEffect(() => {
    if (!playing) return;
    const timer = setInterval(() => setMonth((current) => Math.min((current ?? -1) + 1, months.length - 1)), STEP_MS);
    return () => clearInterval(timer);
  }, [playing, months.length]);
  useEffect(() => {
    if (playing && month === months.length - 1) setPlaying(false);
  }, [playing, month, months.length]);

  const select = (point: AtlasPoint) => {
    setSelected(point);
    setAreaPoint(point.places ? point : null);
  };
  const play = () => {
    if (month == null || month >= months.length - 1) setMonth(0);
    setPlaying(true);
  };

  if (atlas.isError && !data) return <Failure onRetry={() => void atlas.refetch()}>地图读取失败。</Failure>;

  return <div className={styles.footprint}>
    <div className={styles.filters}>
      <Select aria-label="平台" value={provider} onChange={(event) => setProvider(event.target.value)}>
        <option value="">全部平台</option><option value="wechat">微信</option><option value="instagram">Instagram</option><option value="linkedin">LinkedIn</option>
      </Select>
      <Select aria-label="地点类型" value={kind} onChange={(event) => setKind(event.target.value)}>
        <option value="all">资料城市和动态足迹</option><option value="profile">资料城市</option><option value="post">动态足迹</option>
      </Select>
      <Select aria-label="动态年份" value={year} disabled={kind === "profile"} onChange={(event) => setYear(event.target.value)}>
        <option value="">全部年份</option>{data?.years.map((value) => <option key={value}>{value}</option>)}
      </Select>
      <div className={styles.find}>
        <SearchInput aria-label="查找地点或人" placeholder="城市或人…" value={query} onChange={(event) => { setQuery(event.target.value); setResults(true); }} />
        {query && results && <div className={styles.results} role="listbox" aria-label="匹配的地点">
          {matches.slice(0, 30).map((point, index) => <button key={index} type="button" role="option" aria-selected={false}
            onClick={() => { select(point); setResults(false); }}>
            <span>{point.label}</span><small>{point.people_count} 人 · {point.source_kind === "profile" ? "资料城市" : "动态足迹"}</small>
          </button>)}
          {!matches.length && <Faint>没有匹配的已定位地点。</Faint>}
        </div>}
      </div>
    </div>

    {!data ? <Skeleton height={520} /> : <>
      <p className={styles.counts}>
        <span><b>{formatNumber(data.people)}</b> 位有位置的人</span>
        <span><b>{formatNumber(data.place_count)}</b> 个地点</span>
        <span>{formatNumber(data.profile_records)} 份资料地点 · {formatNumber(data.post_records)} 条动态足迹</span>
        {atlas.isFetching && <small role="status">更新中…</small>}
      </p>

      {months.length > 1 && kind !== "profile" && <div className={styles.playback} data-active={month != null ? "" : undefined}>
        <IconButton label={playing ? "暂停" : "按月回放足迹"} outlined onClick={playing ? () => setPlaying(false) : play}>
          {playing ? <Pause size={16} /> : <Play size={16} />}
        </IconButton>
        <input type="range" min={0} max={months.length - 1} step={1} value={month ?? months.length - 1} aria-label="回放月份"
          aria-valuetext={monthLabel(months[month ?? months.length - 1])}
          onChange={(event) => { setPlaying(false); setMonth(Number(event.target.value)); }} />
        <span className={styles.month}>{month == null ? `${monthLabel(months[0])} – ${monthLabel(months[months.length - 1])}` : monthLabel(months[month])}</span>
        {month != null && <IconButton label="退出回放" size="sm" onClick={() => { setPlaying(false); setMonth(null); }}><X size={14} /></IconButton>}
      </div>}

      <div className={styles.body}>
        <FootprintWorld key={theme} points={data.points} selected={selected} onSelect={select} playback={playback} />
        <aside className={styles.panel} aria-live="polite">
          {selected ? <Selection point={selected} area={areaPoint} onBack={() => setSelected(areaPoint)} onPick={setSelected} />
            : month != null ? <MonthList label={monthLabel(months[month])} points={now} month={months[month]} onPick={select} />
              : null}
        </aside>
      </div>

      <div className={styles.legend}>
        <span><SourceMark provider="wechat" size={10} decorative />微信</span>
        <span><SourceMark provider="instagram" size={10} decorative />Instagram</span>
        <span><SourceMark provider="linkedin" size={10} decorative />LinkedIn</span>
      </div>

      {data.unresolved.length > 0 && <Unlocated rows={data.unresolved} />}
      <p className={styles.attribution}>资料城市参考 <a href="https://www.geonames.org/" target="_blank" rel="noreferrer">GeoNames</a>
        {" · "}<a href="https://creativecommons.org/licenses/by/4.0/" target="_blank" rel="noreferrer">CC BY 4.0</a>。有同名歧义的城市不自动定位。</p>
    </>}
  </div>;
}

function MonthList({ label, points, month, onPick }: { label: string; points: AtlasPoint[]; month: string; onPick: (point: AtlasPoint) => void }) {
  return <div className={styles.selection}>
    <h3>{label}</h3>
    <p>{points.length ? `${points.length} 个地点有动态` : "这个月没有带地点的动态。"}</p>
    {points.length > 0 && <div className={styles.places}>{points.slice(0, AREA_PLACES).map((point, index) => <button key={index} type="button" onClick={() => onPick(point)}>
      <span>{point.label}</span><small>{point.people.slice(0, 2).map((person) => person.name).join("、")} · {point.months![month]} 条</small>
    </button>)}</div>}
  </div>;
}

function Selection({ point, area, onBack, onPick }: { point: AtlasPoint; area: AtlasPoint | null; onBack: () => void; onPick: (point: AtlasPoint) => void }) {
  const shown = Math.min(point.people.length, AREA_PEOPLE);
  return <div className={styles.selection}>
    {area && point !== area && <Button size="sm" variant="ghost" onClick={onBack}>‹ 返回{area.label}</Button>}
    <h3>{point.label}</h3>
    <p>{point.places ? `附近 ${point.places.length} 个地点` : point.source_kind === "profile" ? "资料城市中心 · GeoNames" : "动态记录坐标"}
      {" · "}{point.people_count} 人 · {point.count} 条记录</p>
    {point.places && <div className={styles.places}>
      {point.places.slice(0, AREA_PLACES).map((place, index) => <button key={index} type="button" onClick={() => onPick(place)}>
        <span>{place.label}</span><small>{place.people_count} 人 · {place.count} 条</small>
      </button>)}
      {point.places.length > AREA_PLACES && <Faint>还有 {point.places.length - AREA_PLACES} 个地点，放大地图查看</Faint>}
    </div>}
    <div className={styles.people}>{point.people.slice(0, AREA_PEOPLE).map((person) => <Link key={person.id} href={`/profiles/${person.id}`}>{person.name}</Link>)}</div>
    {point.people_count > shown && <Faint>共 {point.people_count} 人，显示其中 {shown} 位。</Faint>}
    {point.source_kind !== "profile" && point.events.length > 0 && <ol className={styles.events}>
      {point.events.slice(0, AREA_EVENTS).map((event, index) => <li key={index}>
        <time>{event.at.slice(0, 10)}</time><Link href={event.url}>{event.text || "查看动态"}</Link>
      </li>)}
    </ol>}
  </div>;
}

function Unlocated({ rows }: { rows: Atlas["unresolved"] }) {
  const [open, setOpen] = useState(false);
  return <details className={styles.unlocated} onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>尚未精确定位的资料地点 · {rows.length} 种文字地址</summary>
    <Faint>只有国家、区域或重名城市的资料保留原文，等待明确坐标。</Faint>
    {open && <div className={styles.unlocatedRows}>{rows.map((row) => <article key={row.label}>
      <strong>{row.label}</strong><small>{row.count} 人</small>
      <div>{row.people.map((person) => <Link key={person.id} href={`/profiles/${person.id}`}>{person.name}</Link>)}</div>
    </article>)}</div>}
  </details>;
}
