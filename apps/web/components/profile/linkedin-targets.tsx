"use client";

import { ExternalLink, Plus } from "lucide-react";
import { useEffect, useState } from "react";

import { apiFetch } from "../../lib/api";
import { Button } from "../ui/button";
import { Field, Input } from "../ui/field";
import { Failure, Muted, SourceMark } from "../ui/primitives";
import styles from "./dialogs.module.css";

type Target = { account_id: string; external_id: string; profile_url: string | null };

export function LinkedInTargets({ profileId, onChanged, onBusy }: {
  profileId: string; onChanged: () => void; onBusy: (busy: boolean) => void;
}) {
  const [account, setAccount] = useState("");
  const [targets, setTargets] = useState<Target[]>([]);
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState("");
  const endpoint = `/api/v1/profiles/${profileId}/linkedin-targets`;

  useEffect(() => {
    const abort = new AbortController();
    setLoaded(false);
    apiFetch(endpoint, { signal: abort.signal }).then(async (response) => {
      if (!response.ok) throw new Error();
      const rows: Target[] = await response.json();
      if (!abort.signal.aborted) { setTargets(rows); setLoaded(true); }
    }).catch(() => { if (!abort.signal.aborted) setError("暂时无法读取 LinkedIn 采集名单，请重试。"); });
    return () => abort.abort();
  }, [endpoint, revision]);

  async function save(target?: Target) {
    if (busy) return;
    setBusy(true); onBusy(true); setError(""); setSaved("");
    try {
      const response = await apiFetch(target ? `${endpoint}/${target.account_id}` : endpoint, {
        method: target ? "DELETE" : "PUT", headers: { "Content-Type": "application/json" },
        ...(target ? {} : { body: JSON.stringify({ account: account.trim() }) }),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => null);
        throw new Error(typeof body?.detail === "string" ? body.detail : "保存失败，请重试。");
      }
      setSaved(target ? "已移出手动采集名单。已有资料和账号关联会保留；connection 名单仍会独立采集。"
        : "已加入采集名单，将随后续 LinkedIn 采集任务更新。");
      if (!target) setAccount("");
      setRevision((value) => value + 1);
      onChanged();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "保存失败，请重试。");
    } finally {
      setBusy(false); onBusy(false);
    }
  }

  return <section className={styles.linkedinTargets} aria-label="LinkedIn 持续采集">
    <h3><SourceMark provider="linkedin" size={12} decorative />LinkedIn 持续采集</h3>
    <Muted>添加此人的 LinkedIn 账号，后续采集会自动纳入名单。无需成为 connection；仅采集当前可访问的资料。</Muted>
    <form onSubmit={(event) => { event.preventDefault(); void save(); }}>
      <Field label="LinkedIn 账号" hint="粘贴个人主页链接，或填写 linkedin.com/in/ 后的账号。">
        <Input value={account} onChange={(event) => setAccount(event.target.value)}
          placeholder="https://www.linkedin.com/in/username" autoComplete="off" maxLength={2048} disabled={busy} required />
      </Field>
      <div className={styles.actions}><Button variant="primary" type="submit" disabled={busy || !account.trim()}>
        <Plus size={14} />{busy ? "保存中…" : "添加并持续采集"}
      </Button></div>
    </form>
    {error && <Failure>{error}<Button size="sm" disabled={busy} onClick={() => { setError(""); setRevision((value) => value + 1); }}>重新读取</Button></Failure>}
    {saved && <div role="status"><Muted>{saved}</Muted></div>}
    {targets.length > 0 && <ul className={styles.accounts}>{targets.map((target) => <li key={target.account_id}>
      <div><a href={target.profile_url || `https://www.linkedin.com/in/${encodeURIComponent(target.external_id)}`} target="_blank" rel="noreferrer">
        {target.external_id} <ExternalLink size={12} aria-hidden="true" />
      </a><small>已加入后续采集</small></div>
      <Button size="sm" disabled={busy} onClick={() => void save(target)}>停止手动采集</Button>
    </li>)}</ul>}
    {loaded && !targets.length && <Muted>尚未添加手动采集账号。</Muted>}
  </section>;
}
