"use client";

import { Plus, X } from "lucide-react";
import { type FormEvent, useEffect, useState } from "react";

import { apiFetch } from "../lib/api";
import styles from "./relationship-editor.module.css";
import { Button, IconButton } from "./ui/button";
import { Field, Input, Textarea } from "./ui/field";
import { Failure } from "./ui/primitives";

type Person = { id: string; display_name: string; profile_type: string };

/** Adds a typed relationship to one specific, confirmed person. */
export function RelationshipEditor({ profileId, onSaved }: { profileId: string; onSaved: () => void }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [matches, setMatches] = useState<Person[]>([]);
  const [person, setPerson] = useState<Person | null>(null);
  const [kind, setKind] = useState("朋友");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!query.trim()) {
      setMatches([]);
      return;
    }
    const abort = new AbortController();
    const timer = setTimeout(() => fetch(`/api/v1/profiles?q=${encodeURIComponent(query)}&profile_type=person&limit=8`, { signal: abort.signal })
      .then((response) => response.json()).then(setMatches).catch(() => undefined), 250);
    return () => {
      clearTimeout(timer);
      abort.abort();
    };
  }, [query]);
  async function save(event: FormEvent) {
    event.preventDefault();
    if (!person) return;
    setBusy(true);
    setError("");
    const response = await apiFetch(`/api/v1/profiles/${profileId}/relationships`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ to_profile_id: person.id, relationship_type: kind, note }),
    }).finally(() => setBusy(false));
    if (!response.ok) {
      setError("关系未保存，请重试。");
      return;
    }
    setOpen(false);
    setPerson(null);
    setQuery("");
    onSaved();
  }
  if (!open) return <Button size="sm" onClick={() => setOpen(true)}><Plus size={14} />添加关系</Button>;
  return <form className={styles.form} onSubmit={save}>
    <header><strong>添加关系</strong><IconButton size="sm" label="关闭添加" onClick={() => setOpen(false)}><X size={15} /></IconButton></header>
    {person
      ? <p className={styles.chosen}>已选择：<strong>{person.display_name}</strong><Button variant="ghost" size="sm" onClick={() => setPerson(null)}>重新选择</Button></p>
      : <Field label="选择联系人"><Input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索并确认具体的人" autoFocus /></Field>}
    {!person && matches.length > 0 && <div className={styles.matches}>{matches.filter((match) => match.id !== profileId).map((match) =>
      <button type="button" key={match.id} onClick={() => { setPerson(match); setMatches([]); }}>{match.display_name}</button>)}</div>}
    <Field label="关系类型"><Input required maxLength={120} value={kind} onChange={(event) => setKind(event.target.value)} placeholder="如同事、同学、家人" /></Field>
    <Field label="备注"><Textarea rows={2} value={note} onChange={(event) => setNote(event.target.value)} /></Field>
    {error && <Failure>{error}</Failure>}
    <Button type="submit" variant="primary" disabled={busy || !person}>{busy ? "保存中…" : "确认关系"}</Button>
  </form>;
}
