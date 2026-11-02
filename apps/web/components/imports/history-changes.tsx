"use client";

import { useCallback, useEffect, useState } from "react";
import { Button } from "../ui/button";
import { Dialog } from "../ui/dialog";
import { Failure, formatDateTime, Loading, providerLabel, Section } from "../ui/primitives";
import { RawData } from "./raw-data";
import styles from "./imports.module.css";

type Event = { id: number; provider: string; relation_name: string; operation: string; committed_at: string };
type Detail = Event & { changed_fields: { field: string; before: unknown; after: unknown; had_before: boolean; has_after: boolean }[] };
const tables: Record<string, string> = { profiles: "人物资料", identities: "平台身份", source_account_links: "账户关联",
  profile_field_current: "资料字段", profile_structured_current: "结构化资料", activities: "活动",
  media_links: "图片与内容关联", accounts: "来源账户", account_identifiers: "来源标识", objects: "来源内容" };
const fields: Record<string, string> = { display_name: "名称", summary: "简介", profile_id: "人物", status: "状态",
  username: "用户名", current_hash: "内容版本", avatar_media_id: "头像", title: "标题", body: "正文",
  "instagram.biography": "Instagram 简介", "wechat.signature": "微信签名", "linkedin.headline": "LinkedIn 简介" };
const actions: Record<string, string> = { I: "新增", U: "更新", D: "删除", T: "清空" };
const value = (item: unknown, exists: boolean) => !exists ? "—" : item === null ? "空值" : typeof item === "string" ? item : JSON.stringify(item, null, 2);

export function HistoryChanges() {
  const [events, setEvents] = useState<Event[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [hasMore, setHasMore] = useState(false);
  const [selected, setSelected] = useState<Detail | null>(null);
  const [provider, setProvider] = useState("crm");
  const load = useCallback(async (before?: number) => {
    setLoading(true);
    try {
      const response = await fetch(`/api/v1/cdc/events?limit=50${before ? `&before=${before}` : ""}${provider ? `&provider=${provider}` : ""}`);
      if (response.status === 404) { setEnabled(false); return; }
      if (!response.ok) throw new Error("history");
      const rows = await response.json() as Event[];
      setEvents(current => before ? [...current, ...rows] : rows);
      setHasMore(rows.length === 50);
      setError("");
    } catch { setError("暂时无法读取历史变化，请重试。当前内容仍可正常查看。"); }
    finally { setLoading(false); }
  }, [provider]);
  useEffect(() => { void load(); }, [load]);
  async function inspect(event: Event) {
    try {
      const response = await fetch(`/api/v1/cdc/events/${event.id}`);
      if (!response.ok) throw new Error("detail");
      setSelected(await response.json() as Detail);
    } catch { setError("暂时无法读取这条变化的详情，请重试。"); }
  }
  if (!enabled) return null;
  return <Section title="历史变化" className={styles.panel}
    action={<div className={styles.actions}><select aria-label="历史变化来源" value={provider} onChange={event => setProvider(event.target.value)}>
      <option value="crm">Monica</option><option value="wechat">微信来源</option><option value="instagram">Instagram 来源</option><option value="linkedin">LinkedIn 来源</option><option value="">所有来源</option>
    </select><Button size="sm" variant="ghost" disabled={loading} onClick={() => void load()}>刷新历史</Button></div>}>
    <p className={styles.note}>历史记录异步整理，可能稍晚出现。这里显示数据库提交时间，不影响内容导入。</p>
    {error && <Failure onRetry={() => void load()}>{error}</Failure>}
    {loading && events.length === 0 ? <Loading /> : <ul className={styles.batches}>
      {events.map(event => <li key={event.id}>
        <div><strong>{tables[event.relation_name] ?? event.relation_name}</strong>
          <small>{event.provider === "crm" ? "Monica" : providerLabel(event.provider)}</small></div>
        <span className={styles.stage}>{actions[event.operation] ?? event.operation}</span>
        <time>{formatDateTime(event.committed_at)}</time>
        <Button size="sm" variant="ghost" onClick={() => void inspect(event)}>查看变化</Button>
      </li>)}
    </ul>}
    {!loading && events.length === 0 && <p className={styles.none}>开启历史记录后产生的变化会显示在这里。较早的数据仍可查看原有字段历史。</p>}
    {hasMore && <Button size="sm" variant="ghost" disabled={loading} onClick={() => void load(events.at(-1)?.id)}>更早的变化</Button>}
    {selected && <Dialog title={`${tables[selected.relation_name] ?? selected.relation_name} · ${actions[selected.operation] ?? selected.operation}`} onClose={() => setSelected(null)}>
      <p className={styles.note}>{formatDateTime(selected.committed_at)}</p>
      <dl className={styles.historyDiff}>{selected.changed_fields.map(field => <div key={field.field}>
        <dt>{fields[field.field] ?? field.field}</dt><dd>
          <div><small>修改前</small><pre>{value(field.before, field.had_before)}</pre></div>
          <div><small>修改后</small><pre>{value(field.after, field.has_after)}</pre></div>
        </dd></div>)}</dl>
      <RawData value={selected} label="查看原始变化记录" />
    </Dialog>}
  </Section>;
}
