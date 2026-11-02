"use client";

import { Cake, CalendarDays, Heart, type LucideIcon, Plus } from "lucide-react";
import { type FormEvent, useRef, useState } from "react";

import type { DateKind, DatePrecision, SignificantDate } from "../../lib/api-types";
import { DateRejected, useDeleteDate, useSaveDate } from "../../lib/queries";
import { Button } from "../ui/button";
import { Checkbox, Input, Select } from "../ui/field";
import { Section } from "../ui/primitives";
import { Segmented } from "../ui/segmented";
import styles from "./dates.module.css";

const KINDS: { id: DateKind; label: string; icon: LucideIcon }[] = [
  { id: "birthday", label: "生日", icon: Cake },
  { id: "anniversary", label: "纪念日", icon: Heart },
  { id: "other", label: "其他", icon: CalendarDays },
];

const ERROR_TEXT: Record<string, string> = {
  missing_month_day: "请填写月和日；只知道年份或月份时，勾选“只记得大概”。",
  approximate_empty: "至少填写年份或月份。",
  day_without_month: "填写了日就要选择月份。",
  invalid_day: "这个月份没有这一天。",
  future: "生日不能晚于今天。",
  year_range: "年份需要在 1800 到 2200 之间。",
  birthday_exists: "这个人已经记录了生日。",
  label_length: "名称最多 60 个字。",
};

export function dateTitle(date: { kind: DateKind; label?: string | null }): string {
  return date.label || KINDS.find((kind) => kind.id === date.kind)!.label;
}

export function dateText(date: Pick<SignificantDate, "precision" | "year" | "month" | "day">): string {
  const parts = `${date.year ? `${date.year}年` : ""}${date.month ? `${date.month}月` : ""}${date.day ? `${date.day}日` : ""}`;
  return date.precision === "approximate" ? `约 ${parts}` : parts;
}

/** Age or anniversary count, then how soon it comes round again. */
export function dateMeta(date: SignificantDate): string {
  const years = date.years == null ? null
    : date.kind === "birthday" ? `${date.years_exact ? "" : "约 "}${date.years} 岁`
    : date.kind === "anniversary" ? `${date.years} 周年` : null;
  const soon = date.days_until == null ? null : date.days_until === 0 ? "就是今天" : date.days_until <= 45 ? `${date.days_until} 天后` : null;
  return [years, soon].filter(Boolean).join(" · ");
}

/** Within a week, and today itself, get the highlighter. */
export const isSoon = (date: Pick<SignificantDate, "days_until">) => date.days_until != null && date.days_until <= 7;

export function SignificantDates({ profileId, dates }: { profileId: string; dates: SignificantDate[] }) {
  const [editing, setEditing] = useState<string | "new" | null>(null);
  const hasBirthday = dates.some((date) => date.kind === "birthday");
  const done = () => setEditing(null);
  return <Section title="重要日子" icon={<CalendarDays size={15} aria-hidden="true" />}
    action={dates.length > 0 && editing === null && <Button variant="ghost" size="sm" onClick={() => setEditing("new")}><Plus size={13} />添加</Button>}>
    {dates.length === 0 && editing === null && <button className={styles.empty} type="button" onClick={() => setEditing("new")}>
      <Plus size={14} />添加生日、纪念日或其他日子
    </button>}
    {(dates.length > 0 || editing === "new") && <ul className={styles.list}>
      {dates.map((date) => <li key={date.id}>{editing === date.id
        ? <DateForm profileId={profileId} date={date} birthdayTaken={false} onDone={done} onCancel={done} />
        : <DateRow date={date} onEdit={() => setEditing(date.id)} />}</li>)}
      {editing === "new" && <li><DateForm profileId={profileId} birthdayTaken={hasBirthday} onDone={done} onCancel={done} /></li>}
    </ul>}
  </Section>;
}

function DateRow({ date, onEdit }: { date: SignificantDate; onEdit: () => void }) {
  const Icon = KINDS.find((kind) => kind.id === date.kind)!.icon;
  const meta = dateMeta(date);
  return <button className={styles.row} type="button" onClick={onEdit} aria-label={`编辑${dateTitle(date)}`} data-soon={isSoon(date) ? "" : undefined}>
    <Icon size={15} aria-hidden="true" />
    <span><small>{dateTitle(date)}</small><strong>{dateText(date)}</strong></span>
    {meta && <em>{meta}</em>}
  </button>;
}

function daysInMonth(year: number | null, month: number | null): number {
  if (!month) return 31;
  return new Date(year && month === 2 ? year : 2024, month, 0).getDate();
}

function precisionOf(approximate: boolean, year: number | null, month: number | null, day: number | null): DatePrecision | string {
  if (approximate) return !year && !month ? "approximate_empty" : "approximate";
  if (year && month && day) return "exact";
  if (month && day) return "month_day";
  return "missing_month_day";
}

function DateForm({ profileId, date, birthdayTaken, onDone, onCancel }: {
  profileId: string; date?: SignificantDate; birthdayTaken: boolean; onDone: () => void; onCancel: () => void;
}) {
  const [kind, setKind] = useState<DateKind>(date?.kind ?? (birthdayTaken ? "anniversary" : "birthday"));
  const [label, setLabel] = useState(date?.label ?? "");
  const [year, setYear] = useState(date?.year ? String(date.year) : "");
  const [month, setMonth] = useState(date?.month ? String(date.month) : "");
  const [day, setDay] = useState(date?.day ? String(date.day) : "");
  const [approximate, setApproximate] = useState(date?.precision === "approximate");
  const [error, setError] = useState("");
  const saveDate = useSaveDate(profileId);
  const deleteDate = useDeleteDate(profileId);
  const busy = saveDate.isPending || deleteDate.isPending;
  // Blocks a second submit that lands before the disabled button re-renders.
  const submitting = useRef(false);
  const numbers = { year: year ? Number(year) : null, month: month ? Number(month) : null, day: day ? Number(day) : null };
  const dayLimit = daysInMonth(numbers.year, numbers.month);
  const failed = (exc: Error) => setError(ERROR_TEXT[exc instanceof DateRejected ? exc.code : ""] || "没能保存，请再试一次。");

  function save(event: FormEvent) {
    event.preventDefault();
    if (submitting.current) return;
    setError("");
    const precision = precisionOf(approximate, numbers.year, numbers.month, numbers.day);
    if (ERROR_TEXT[precision]) return setError(ERROR_TEXT[precision]);
    const body = { kind, label: kind === "birthday" ? null : label.trim() || null, precision,
      year: precision === "month_day" ? null : numbers.year, month: numbers.month, day: numbers.day };
    submitting.current = true;
    saveDate.mutate({ previous: date, body }, { onSuccess: onDone, onError: failed, onSettled: () => { submitting.current = false; } });
  }

  return <form className={styles.form} onSubmit={save}>
    <Segmented label="类型" size="sm" value={kind} onChange={(value) => { setKind(value); setError(""); }}
      options={KINDS.map((choice) => ({ value: choice.id, label: choice.label,
        title: choice.id === "birthday" && birthdayTaken ? "已经记录了生日" : undefined, disabled: choice.id === "birthday" && birthdayTaken }))} />
    {kind !== "birthday" && <Input value={label} maxLength={60} onChange={(event) => setLabel(event.target.value)}
      placeholder={kind === "anniversary" ? "名称，如 结婚纪念日" : "名称，如 第一次见面"} aria-label="名称" />}
    <div className={styles.parts}>
      <Input inputMode="numeric" maxLength={4} placeholder="年" aria-label="年（可选）" value={year}
        onChange={(event) => { setYear(event.target.value.replace(/\D/g, "").slice(0, 4)); setError(""); }} />
      <Select aria-label="月" value={month} onChange={(event) => {
        setMonth(event.target.value);
        setError("");
        if (day && Number(day) > daysInMonth(numbers.year, Number(event.target.value) || null)) setDay("");
      }}>
        <option value="">月</option>
        {Array.from({ length: 12 }, (_, index) => <option key={index + 1} value={index + 1}>{index + 1}月</option>)}
      </Select>
      <Select aria-label="日" value={day} disabled={!month} onChange={(event) => { setDay(event.target.value); setError(""); }}>
        <option value="">日</option>
        {Array.from({ length: dayLimit }, (_, index) => <option key={index + 1} value={index + 1}>{index + 1}日</option>)}
      </Select>
    </div>
    <Checkbox checked={approximate} onChange={(event) => { setApproximate(event.target.checked); setError(""); }}>只记得大概</Checkbox>
    {error && <p className={styles.error} role="alert">{error}</p>}
    <div className={styles.actions}>
      {date && <Button variant="ghost" size="sm" className={styles.remove} disabled={busy}
        onClick={() => deleteDate.mutate(date, { onSuccess: onDone, onError: failed })}>删除</Button>}
      <Button size="sm" disabled={busy} onClick={onCancel}>取消</Button>
      <Button size="sm" variant="primary" type="submit" disabled={busy}>{busy ? "保存中…" : "保存"}</Button>
    </div>
  </form>;
}
