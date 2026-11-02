"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { type FormEvent, useState } from "react";

import { apiFetch } from "../../lib/api";
import { directoryKey } from "../../lib/directory";
import { Button } from "../ui/button";
import { Dialog } from "../ui/dialog";
import { Field, Input } from "../ui/field";
import { Segmented } from "../ui/segmented";
import styles from "./contacts.module.css";

export function CreateProfileDialog({ onClose }: { onClose: () => void }) {
  const client = useQueryClient();
  const router = useRouter();
  const [name, setName] = useState("");
  const [kind, setKind] = useState<"person" | "group">("person");
  const [summary, setSummary] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function create(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    const response = await apiFetch("/api/v1/profiles", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ display_name: name.trim(), profile_type: kind, summary: summary.trim() || null }),
    });
    setBusy(false);
    if (!response.ok) {
      setError("没有创建成功，请再试一次");
      return;
    }
    const profile = await response.json();
    void client.invalidateQueries({ queryKey: directoryKey, exact: true });
    router.push(`/profiles/${profile.id}`);
  }

  return <Dialog title="新建联系人" onClose={onClose} locked={busy}>
    <form className={styles.create} onSubmit={(event) => void create(event)}>
      <Segmented label="类型" value={kind} onChange={setKind} options={[{ value: "person", label: "个人" }, { value: "group", label: "群组" }]} />
      <Field label="名称"><Input autoFocus required value={name} onChange={(event) => setName(event.target.value)} /></Field>
      <Field label="简介" hint="可选，一句话说明是谁"><Input value={summary} onChange={(event) => setSummary(event.target.value)} /></Field>
      {error && <p role="alert" className={styles.error}>{error}</p>}
      <div className={styles.createActions}>
        <Button variant="ghost" onClick={onClose} disabled={busy}>取消</Button>
        <Button variant="primary" type="submit" disabled={busy || !name.trim()}>{busy ? "正在创建…" : "创建"}</Button>
      </div>
    </form>
  </Dialog>;
}
