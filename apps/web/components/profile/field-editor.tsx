"use client";

import { Save } from "lucide-react";
import { type FormEvent, useState } from "react";

import { apiFetch } from "../../lib/api";
import { Button } from "../ui/button";
import { Field, Input } from "../ui/field";
import { MarkdownEditor } from "../ui/markdown";
import { Faint, Failure, Panel } from "../ui/primitives";
import styles from "./profile.module.css";
import type { Profile } from "./types";

/** Name and summary; every other detail is edited row by row in 笔记与资料. Each change becomes a new manual version. */
export function FieldEditor({ profileId, profile, onSaved, onCancel }: { profileId: string; profile: Profile; onSaved: () => void; onCancel: () => void }) {
  const [name, setName] = useState(profile.display_name);
  const [summary, setSummary] = useState(profile.summary || "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function save(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    const changes = [["display_name", name.trim()], ["summary", summary]].filter(([key, value]) => value !== (key === "display_name" ? profile.display_name : profile.summary || ""));
    try {
      for (const [field_key, value] of changes) {
        const response = await apiFetch(`/api/v1/profiles/${profileId}/fields`, {
          method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ field_key, value }),
        });
        if (!response.ok) throw new Error(String(response.status));
      }
      onSaved();
    } catch {
      setError("保存失败，请稍后重试。");
    } finally {
      setBusy(false);
    }
  }
  return <Panel className={styles.editor} label="编辑姓名和简介">
    <form className={styles.editorForm} onSubmit={save} onKeyDown={(event) => { if (event.key === "Escape") onCancel(); }}>
      <div className={styles.editorRow}>
        <Field label={profile.profile_type === "group" ? "群名" : "姓名"}><Input required value={name} onChange={(event) => setName(event.target.value)} autoFocus /></Field>
        <Faint>电话、籍贯等其他资料在「笔记与资料」里逐条编辑</Faint>
      </div>
      <MarkdownEditor label="简介" value={summary} onChange={setSummary} placeholder="一两句话介绍这个人…" />
      {error && <Failure>{error}</Failure>}
      <div className={styles.editorActions}>
        <Button onClick={onCancel}>取消</Button>
        <Button type="submit" variant="primary" disabled={busy || !name.trim()}><Save size={14} />{busy ? "保存中…" : "保存"}</Button>
      </div>
    </form>
  </Panel>;
}
