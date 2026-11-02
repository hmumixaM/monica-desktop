"use client";

import { ArrowRight } from "lucide-react";
import { useMemo } from "react";

import { useProfileTab } from "../../lib/queries";
import { Failure, Loading, Muted, Panel } from "../ui/primitives";
import { fieldLabel, formatDate, summarizeValue, visibleField } from "./fields";
import styles from "./profile.module.css";
import type { Revision } from "./types";

const SHOWN = 120;

/** Field versions with the previous value from the same source beside each change. */
export function HistoryView({ profileId }: { profileId: string }) {
  const history = useProfileTab<Revision[]>(profileId, "history");
  const meaningful = useMemo(() => (history.data ?? []).flatMap((entry) => entry.field_key === "monica.custom_fields" && entry.value && typeof entry.value === "object"
    ? Object.entries(entry.value as Record<string, unknown>).map(([key, value]) => ({ ...entry, id: `${entry.id}:${key}`, field_key: `Monica · ${key}`, value }))
    : [entry]).filter((entry) => (entry.operation === "manual_delete" && entry.field_key !== "monica.custom_fields") || visibleField(entry)), [history.data]);
  const rows = meaningful.slice(0, SHOWN).map((entry, index) => ({
    entry, previous: meaningful.slice(index + 1).find((candidate) => candidate.field_key === entry.field_key && candidate.source_type === entry.source_type),
  }));
  return <Panel className={styles.tabPanel}>
    <header className={styles.tabHeader}><h2>字段变更记录</h2><small>{meaningful.length} 个资料版本</small></header>
    {history.isPending && <Loading />}
    {history.isError && <Failure onRetry={() => void history.refetch()}>历史读取失败。</Failure>}
    {history.isSuccess && !rows.length && <Muted>还没有资料变更。</Muted>}
    {rows.length > 0 && <ol className={styles.history}>{rows.map(({ entry, previous }) => <li key={entry.id}>
      <div><strong>{fieldLabel(entry.field_key)}</strong><small>{entry.source_type}{entry.is_conflict ? " · 冲突候选" : ""} · {formatDate(entry.observed_at)}</small></div>
      <div className={styles.diff}>
        <span data-from="">{previous ? summarizeValue(previous.value) : "首次记录"}</span>
        <ArrowRight size={13} aria-hidden="true" />
        <span data-to="">{entry.operation === "manual_delete" ? "已删除" : summarizeValue(entry.value)}</span>
      </div>
    </li>)}</ol>}
  </Panel>;
}
