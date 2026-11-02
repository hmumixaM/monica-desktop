"use client";

import { Check, Edit3, Plus, RotateCcw } from "lucide-react";
import Link from "next/link";
import { type FormEvent, useEffect, useRef, useState } from "react";

import { apiFetch } from "../../lib/api";
import { usePersonPicker } from "../../lib/directory";
import { recordText } from "../../lib/record-text";
import { type Attachment, AttachmentList, AttachmentPicker } from "../media-attachments";
import { Button } from "../ui/button";
import { Chip, ChipButton } from "../ui/chip";
import { Field, Input, Select } from "../ui/field";
import { LoadMore } from "../ui/load-more";
import { Clamp, Markdown, MarkdownEditor } from "../ui/markdown";
import { Failure, Loading, Muted, Panel, formatDateTime } from "../ui/primitives";
import styles from "./records.module.css";

type Person = { id: string; display_name: string };
type RecordItem = {
  id: string; activity_type: string; occurred_at: string; title: string; body?: string; source_type: string; state: string;
  due_at?: string; participants?: Person[]; media?: Attachment[];
};
const KINDS: Record<string, string> = {
  note: "笔记", activity: "活动", task: "待办", reminder: "提醒", call: "通话", money: "金钱", wechat_call: "微信通话", wechat_money: "微信收支",
};
const PAGE = 30;
const DUE_KINDS = ["task", "reminder"];
const localInput = (value?: string) => {
  const date = value ? new Date(value) : new Date();
  return new Date(date.getTime() - date.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
};

function RecordForm({ profileId, record, onSaved, onCancel }: { profileId: string; record: RecordItem | null; onSaved: () => void; onCancel: () => void }) {
  const [kind, setKind] = useState(record?.activity_type ?? "note");
  const [title, setTitle] = useState(record?.title ?? "");
  const [body, setBody] = useState(record?.body ?? "");
  const [at, setAt] = useState(localInput(record?.occurred_at));
  const [due, setDue] = useState(record?.due_at ? localInput(record.due_at) : "");
  const [media, setMedia] = useState<Attachment[]>(record?.media ?? []);
  const [people, setPeople] = useState<Person[]>(record?.participants?.filter((person) => person.id !== profileId) ?? []);
  const [query, setQuery] = useState("");
  const [uploading, setUploading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const matches = usePersonPicker(query, 8).results.filter((person) => person.id !== profileId && !people.some((chosen) => chosen.id === person.id));

  async function save(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    const response = await apiFetch(record ? `/api/v1/activities/${record.id}` : "/api/v1/activities", {
      method: record ? "PUT" : "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        activity_type: kind, title, body, media_ids: media.map((item) => item.id), occurred_at: new Date(at).toISOString(),
        due_at: due && DUE_KINDS.includes(kind) ? new Date(due).toISOString() : null, participant_profile_ids: [profileId, ...people.map((person) => person.id)],
      }),
    }).finally(() => setBusy(false));
    if (response.ok) onSaved();
    else setError("保存失败，内容仍保留在编辑框中。");
  }

  return <form className={styles.form} onSubmit={save}>
    <div className={styles.formRow}>
      <Field label="类型"><Select value={kind} onChange={(event) => setKind(event.target.value)}>
        {Object.entries(KINDS).filter(([key]) => !key.startsWith("wechat_")).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
      </Select></Field>
      <Field label="时间"><Input required type="datetime-local" value={at} onChange={(event) => setAt(event.target.value)} /></Field>
      {DUE_KINDS.includes(kind) && <Field label="到期"><Input type="datetime-local" value={due} onChange={(event) => setDue(event.target.value)} /></Field>}
    </div>
    <Field label="标题"><Input required maxLength={255} value={title} onChange={(event) => setTitle(event.target.value)} autoFocus /></Field>
    <MarkdownEditor label="内容" value={body} onChange={setBody} placeholder="写下发生了什么、聊了什么…" />
    <AttachmentPicker items={media} onChange={setMedia} onBusy={setUploading} />
    <Field label="参与人"><Input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索联系人并选择" autoComplete="off" /></Field>
    {(people.length > 0 || matches.length > 0) && <div className={styles.people}>
      {people.map((person) => <Chip key={person.id} onRemove={() => setPeople((old) => old.filter((item) => item.id !== person.id))}
        removeLabel={`移除 ${person.display_name}`}>{person.display_name}</Chip>)}
      {matches.map((person) => <ChipButton key={person.id} onClick={() => {
        setPeople((old) => [...old, { id: person.id, display_name: person.name }]);
        setQuery("");
      }}><Plus size={12} />{person.name}</ChipButton>)}
    </div>}
    {error && <Failure>{error}</Failure>}
    <div className={styles.formActions}>
      <Button disabled={uploading || busy} onClick={onCancel}>取消</Button>
      <Button type="submit" variant="primary" disabled={busy || uploading}>{busy ? "保存中…" : "保存记录"}</Button>
    </div>
  </form>;
}

/** Notes, activities, tasks and imported calls or money about this person, newest first. */
export function RecordsView({ profileId }: { profileId: string }) {
  const [rows, setRows] = useState<RecordItem[]>([]);
  const [more, setMore] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [editing, setEditing] = useState<RecordItem | "new" | null>(null);
  const [toggling, setToggling] = useState<string | null>(null);
  const request = useRef<AbortController | null>(null);

  async function load(offset = 0) {
    if (offset && loading) return;
    request.current?.abort();
    const abort = new AbortController();
    request.current = abort;
    setLoading(true);
    setError("");
    try {
      const response = await fetch(`/api/v1/activities?profile_id=${profileId}&limit=${PAGE}&offset=${offset}`, { signal: abort.signal });
      if (!response.ok) throw new Error(String(response.status));
      const data: RecordItem[] = await response.json();
      if (abort.signal.aborted) return;
      setRows((old) => offset ? [...old, ...data.filter((row) => !old.some((item) => item.id === row.id))] : data);
      setMore(data.length === PAGE);
    } catch {
      if (!abort.signal.aborted) setError("无法读取记录，请重试。");
    } finally {
      if (!abort.signal.aborted) setLoading(false);
    }
  }
  useEffect(() => {
    setRows([]);
    setMore(false);
    void load();
    return () => request.current?.abort();
  }, [profileId]);

  async function toggle(row: RecordItem) {
    setToggling(row.id);
    const response = await apiFetch(`/api/v1/activities/${row.id}/state`, {
      method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ state: row.state === "done" ? "open" : "done" }),
    }).finally(() => setToggling(null));
    if (response.ok) await load();
    else setError("无法更新完成状态。");
  }

  return <Panel className={styles.records} label="笔记与记录">
    <header className={styles.header}>
      <h2>笔记与记录</h2>
      {editing === null && <Button variant="primary" onClick={() => setEditing("new")}><Plus size={15} />添加记录</Button>}
    </header>
    {editing !== null && <RecordForm key={editing === "new" ? "new" : editing.id} profileId={profileId} record={editing === "new" ? null : editing}
      onCancel={() => setEditing(null)} onSaved={() => { setEditing(null); void load(); }} />}
    {error && <Failure onRetry={() => void load()}>{error}</Failure>}
    <ol className={styles.list}>{rows.map((row) => {
      const overdue = row.state === "open" && !!row.due_at && new Date(row.due_at) < new Date();
      const text = recordText(row.body);
      // Imported calls and money arrive as JSON and come back as plain lines; only text someone typed is Markdown.
      const written = !!text && text === row.body;
      return <li key={row.id} data-kind={row.activity_type} data-done={row.state === "done" ? "" : undefined}>
        <header>
          <span className={styles.kind}>{KINDS[row.activity_type] || "记录"}</span>
          <time dateTime={row.occurred_at}>{formatDateTime(row.occurred_at, false)}</time>
          {row.source_type !== "manual" && <small>导入</small>}
        </header>
        <h3>{row.title}</h3>
        {written ? <Clamp><Markdown>{text}</Markdown></Clamp> : text && <p>{text}</p>}
        <AttachmentList items={row.media ?? []} />
        {row.due_at && <p className={styles.due} data-overdue={overdue ? "" : undefined}>到期 {formatDateTime(row.due_at)}</p>}
        {!!row.participants?.length && <div className={styles.participants}>{row.participants.filter((person) => person.id !== profileId)
          .map((person) => <Link href={`/profiles/${person.id}`} key={person.id}>{person.display_name}</Link>)}</div>}
        {(DUE_KINDS.includes(row.activity_type) || row.source_type === "manual") && <footer>
          {DUE_KINDS.includes(row.activity_type) && <Button size="sm" variant="ghost" disabled={toggling === row.id} onClick={() => void toggle(row)}>
            {row.state === "done" ? <><RotateCcw size={13} />重新打开</> : <><Check size={13} />标为完成</>}
          </Button>}
          {row.source_type === "manual" && <Button size="sm" variant="ghost" disabled={editing !== null} onClick={() => setEditing(row)}><Edit3 size={13} />编辑</Button>}
        </footer>}
      </li>;
    })}</ol>
    {!rows.length && !loading && !error && editing === null && <Muted>尚无记录。</Muted>}
    {loading && <Loading>正在读取记录…</Loading>}
    {more && <LoadMore cursor={rows.at(-1)?.id ?? ""} busy={loading || editing !== null} error={!!error} onLoad={() => void load(rows.length)} label="加载更多记录" />}
  </Panel>;
}
