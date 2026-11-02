"use client";

import { useQueryClient } from "@tanstack/react-query";
import { Edit3, NotebookPen, Plus, Trash2 } from "lucide-react";
import { type KeyboardEvent, useId, useMemo, useState } from "react";

import { apiFetch } from "../../lib/api";
import { queryKeys } from "../../lib/queries";
import { Button, IconButton } from "../ui/button";
import { ChipButton } from "../ui/chip";
import { Input, Textarea } from "../ui/field";
import { Clamp, Markdown, MarkdownEditor } from "../ui/markdown";
import { Failure, Section } from "../ui/primitives";
import { ReadableValue } from "./about";
import { type DetailRef, type DetailRow, PRESETS, detailRows, presetFor, savePlan } from "./details";
import styles from "./profile.module.css";
import type { Profile } from "./types";

const isUrl = (line: string) => /^https?:\/\/\S+$/.test(line);
const isLong = (value: string) => value.includes("\n") || value.length > 80;

function DetailValue({ value }: { value: string }) {
  const lines = value.split("\n");
  return lines.every(isUrl) ? <ReadableValue value={lines.length > 1 ? lines : value} />
    : <Clamp><Markdown className={styles.factText}>{value}</Markdown></Clamp>;
}

function DetailForm({ profileId, rows, row, onSaved, onCancel }: {
  profileId: string; rows: DetailRow[]; row: DetailRow | null; onSaved: () => Promise<unknown>; onCancel: () => void;
}) {
  const listId = useId();
  const [label, setLabel] = useState(row?.label ?? "");
  const [value, setValue] = useState(row?.values[0] ?? "");
  const [startedLong] = useState(() => isLong(row?.values[0] ?? ""));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const plan = savePlan(rows, row, label);
  const preset = presetFor(label);
  const long = startedLong || !!preset?.multiline;
  const others = [...new Set([...(row?.values.slice(1) ?? []), ...(plan.merges?.values ?? [])])].filter((old) => old !== value.trim());
  const used = new Set(rows.map((item) => item.id));
  const suggestions = PRESETS.filter((item) => !used.has(item.key));

  async function save(event: { preventDefault: () => void }) {
    event.preventDefault();
    if (!label.trim() || !value.trim() || busy) return;
    setBusy(true);
    setError("");
    try {
      const response = await apiFetch(`/api/v1/profiles/${profileId}/details`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ field_key: plan.key, value, replaces: plan.replaces }),
      });
      if (!response.ok) throw new Error(String(response.status));
      await onSaved();
    } catch {
      setError("保存失败，内容仍保留在编辑框中。");
      setBusy(false);
    }
  }
  function keys(event: KeyboardEvent<HTMLFormElement>) {
    if (event.key === "Escape") onCancel();
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) void save(event);
  }

  return <form className={styles.detailForm} onSubmit={save} onKeyDown={keys} aria-label={row ? `编辑${row.label}` : "添加资料"}>
    <Input aria-label="名称" list={listId} value={label} maxLength={120} required autoFocus={!row}
      placeholder="名称，如 电话、籍贯、饮食偏好" onChange={(event) => setLabel(event.target.value)}
      onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); event.currentTarget.form?.querySelector("textarea")?.focus(); } }} />
    <datalist id={listId}>{suggestions.map((item) => <option key={item.key} value={item.label} />)}</datalist>
    {!row && !label && <div className={styles.detailPresets}>
      {suggestions.map((item) => <ChipButton key={item.key} onClick={() => setLabel(item.label)}>{item.label}</ChipButton>)}
    </div>}
    {long
      ? <MarkdownEditor label={label.trim() || "内容"} value={value} onChange={setValue} autoFocus={!!row} placeholder="填写内容…" />
      : <Textarea aria-label={label.trim() || "内容"} className={styles.detailInput} rows={1} value={value} autoFocus={!!row}
        placeholder={preset?.placeholder ?? "填写内容…"} onChange={(event) => setValue(event.target.value)}
        onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey && !event.metaKey && !event.ctrlKey && !event.nativeEvent.isComposing) void save(event); }} />}
    {plan.merges && <p className={styles.detailHint}>已有「{plan.merges.label}」，保存后合并为一条。</p>}
    {others.length > 0 && <div className={styles.detailHint}>
      <p>保存后只保留上面的内容，下面 {others.length} 个旧值移入历史：</p>
      <ul>{others.map((old) => <li key={old}>{old}</li>)}</ul>
      <Button size="sm" variant="ghost" onClick={() => setValue((current) => [current.trim(), ...others].filter(Boolean).join("\n"))}>把旧值并入内容</Button>
    </div>}
    {error && <Failure>{error}</Failure>}
    <div className={styles.detailFormActions}>
      <small>{long ? "⌘/Ctrl + Enter 保存" : "Enter 保存 · Shift + Enter 换行"} · Esc 取消</small>
      <Button size="sm" disabled={busy} onClick={onCancel}>取消</Button>
      <Button size="sm" type="submit" variant="primary" disabled={busy || !label.trim() || !value.trim()}>{busy ? "保存中…" : "保存"}</Button>
    </div>
  </form>;
}

/** Hand-kept details, one row per name: edit or delete any row in place, including those the Monica import bundled together. */
export function ManualDetails({ profile }: { profile: Profile }) {
  const client = useQueryClient();
  const rows = useMemo(() => detailRows(profile.fields ?? []), [profile.fields]);
  const [editing, setEditing] = useState<string | "new" | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const refresh = async () => {
    await client.invalidateQueries({ queryKey: queryKeys.profile(profile.id) });
    setEditing(null);
  };

  async function remove(refs: DetailRef[]) {
    setBusy(true);
    setError("");
    const response = await apiFetch(`/api/v1/profiles/${profile.id}/details`, {
      method: "DELETE", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ refs }),
    }).finally(() => setBusy(false));
    if (!response.ok) return setError("删除失败，请重试。");
    setConfirming(null);
    await client.invalidateQueries({ queryKey: queryKeys.profile(profile.id) });
  }
  const form = (row: DetailRow | null) => <DetailForm key={row?.id ?? "new"} profileId={profile.id} rows={rows} row={row}
    onSaved={refresh} onCancel={() => setEditing(null)} />;

  return <Section title="笔记与资料" icon={<NotebookPen size={15} aria-hidden="true" />}
    action={editing === null && <Button size="sm" variant="ghost" onClick={() => setEditing("new")}><Plus size={13} />添加</Button>}>
    {rows.length > 0 && <dl className={styles.details}>{rows.map((row) => editing === row.id ? <div key={row.id} data-editing="">{form(row)}</div>
      : <div key={row.id} data-long={isLong(row.values[0]) ? "" : undefined}>
        <dt>{row.label}</dt>
        <dd>
          <DetailValue value={row.values[0]} />
          {row.values.length > 1 && <details className={styles.detailOlder}>
            <summary>另有 {row.values.length - 1} 个不同的值</summary>
            {row.values.slice(1).map((old) => <DetailValue key={old} value={old} />)}
          </details>}
          {confirming === row.id && <span className={styles.detailConfirm} role="alert">
            删除「{row.label}」？
            <Button size="sm" variant="danger" disabled={busy} onClick={() => void remove(row.refs)}>{busy ? "删除中…" : "删除"}</Button>
            <Button size="sm" variant="ghost" disabled={busy} onClick={() => setConfirming(null)}>取消</Button>
          </span>}
        </dd>
        <span className={styles.detailActions}>
          <IconButton size="sm" label={`编辑${row.label}`} disabled={editing !== null} onClick={() => { setConfirming(null); setEditing(row.id); }}><Edit3 size={13} /></IconButton>
          <IconButton size="sm" label={`删除${row.label}`} disabled={editing !== null} onClick={() => setConfirming(row.id)}><Trash2 size={13} /></IconButton>
        </span>
      </div>)}</dl>}
    {error && <Failure>{error}</Failure>}
    {editing === "new" ? form(null)
      : !rows.length && <button type="button" className={styles.addLine} onClick={() => setEditing("new")}><Plus size={14} />添加个人笔记或资料</button>}
  </Section>;
}
