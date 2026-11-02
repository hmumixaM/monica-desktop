"use client";

import { Link2, Unlink } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";

import { apiFetch } from "../../lib/api";
import { BLOCKER_LABEL } from "../linking/types";
import { Button } from "../ui/button";
import { Dialog } from "../ui/dialog";
import { Field, Input, Select } from "../ui/field";
import { Failure, Muted, SourceMark } from "../ui/primitives";
import styles from "./dialogs.module.css";
import { LinkedInTargets } from "./linkedin-targets";

type Account = {
  id: string; provider: string; external_id: string; object_kind: string; display_name: string; username?: string; ready: boolean;
  blocked_reason: string | null; profile_id: string | null; linked_profile_name?: string;
};
const LABELS: Record<string, string> = { wechat: "微信", instagram: "Instagram", linkedin: "LinkedIn" };

async function readAccounts(url: string, signal: AbortSignal): Promise<Account[]> {
  const response = await apiFetch(url, { signal });
  if (!response.ok) throw new Error(`Account request failed: ${response.status}`);
  return response.json();
}

/**
 * Which source accounts this profile shows. Linking always passes the account's current owner so a concurrent
 * change is refused instead of silently moved; quarantined accounts cannot be linked from here.
 */
export function SourceAccountsDialog({ profileId, profileType, onChanged, onClose }: {
  profileId: string; profileType: string; onChanged: () => void; onClose: () => void;
}) {
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [results, setResults] = useState<Account[]>([]);
  const [query, setQuery] = useState("");
  const [provider, setProvider] = useState("wechat");
  const [pending, setPending] = useState<{ account: Account; unlink: boolean } | null>(null);
  const [busy, setBusy] = useState(false);
  const [targetBusy, setTargetBusy] = useState(false);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    const abort = new AbortController();
    setError("");
    readAccounts(`/api/v1/source-accounts?profile_id=${profileId}`, abort.signal).then(setAccounts)
      .catch(() => { if (!abort.signal.aborted) setError("暂时无法读取账号。"); });
    return () => abort.abort();
  }, [profileId, revision]);
  useEffect(() => {
    setResults([]);
    if (!query.trim()) return;
    const abort = new AbortController();
    const timer = setTimeout(() => {
      readAccounts(`/api/v1/source-accounts?provider=${provider}&q=${encodeURIComponent(query.trim())}`, abort.signal)
        .then((rows) => setResults(rows.filter((row) => row.object_kind === profileType && row.profile_id !== profileId)))
        .catch(() => { if (!abort.signal.aborted) setError("搜索失败，请重试。"); });
    }, 250);
    return () => {
      clearTimeout(timer);
      abort.abort();
    };
  }, [query, provider, profileId, profileType, revision]);

  async function save() {
    if (!pending) return;
    setBusy(true);
    setError("");
    const response = await apiFetch(`/api/v1/source-accounts/${pending.account.id}/link`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile_id: pending.unlink ? null : profileId, expected_profile_id: pending.account.profile_id }),
    }).finally(() => setBusy(false));
    if (!response.ok) {
      setError("关联未保存。请刷新核对当前归属；来源资料会完整保留。");
      return;
    }
    setPending(null);
    setRevision((value) => value + 1);
    onChanged();
  }

  function row(account: Account, unlink: boolean) {
    const blocked = account.blocked_reason === "quarantined" || (!unlink && !account.ready);
    return <li key={account.id}>
      <SourceMark provider={account.provider} size={12} decorative />
      <div>
        <strong>{account.display_name}</strong>
        <small>{LABELS[account.provider]} · {account.username || account.external_id}</small>
        {account.profile_id && !unlink && <small>当前关联 <Link href={`/profiles/${account.profile_id}`}>{account.linked_profile_name}</Link></small>}
        {blocked && account.blocked_reason && <small data-blocked="">{BLOCKER_LABEL[account.blocked_reason] ?? account.blocked_reason}</small>}
      </div>
      <Button size="sm" disabled={blocked || busy || targetBusy} onClick={() => setPending({ account, unlink })}>
        {unlink ? <Unlink size={14} /> : <Link2 size={14} />}{unlink ? "解除关联" : "关联"}
      </Button>
    </li>;
  }

  return <Dialog title="来源账号" onClose={onClose} locked={busy || targetBusy} closeLabel="关闭来源账号"
   >
    {error && <Failure>{error}</Failure>}
    {pending ? <div className={styles.confirm}>
      <h3>{pending.unlink ? "解除此账号关联" : "确认账号归属"}</h3>
      <p><strong>{LABELS[pending.account.provider]} · {pending.account.display_name}</strong> · {pending.account.username || pending.account.external_id}</p>
      <Muted>{pending.unlink ? "此人物页将不再显示这个账号的内容。人物的手工资料和记录会保留。"
        : pending.account.profile_id ? `此账号将从「${pending.account.linked_profile_name}」解除，并关联到当前人物。` : "确认这个来源账号属于当前人物后关联。"}</Muted>
      <div className={styles.actions}>
        <Button disabled={busy} onClick={() => setPending(null)}>取消</Button>
        <Button variant="primary" disabled={busy} onClick={() => void save()}>{busy ? "保存中…" : "确认"}</Button>
      </div>
    </div> : <>
      {profileType === "person" && <LinkedInTargets profileId={profileId} onBusy={setTargetBusy} onChanged={() => {
        setRevision((value) => value + 1); onChanged();
      }} />}
      {accounts.length ? <ul className={styles.accounts}>{accounts.map((account) => row(account, true))}</ul> : <Muted>当前没有关联来源账号。</Muted>}
      <form className={styles.search} onSubmit={(event) => event.preventDefault()}>
        <Field label="平台"><Select value={provider} onChange={(event) => setProvider(event.target.value)}>
          {Object.entries(LABELS).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
        </Select></Field>
        <Field label="寻找已有账号"><Input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="账号 ID、用户名或显示名" autoComplete="off" /></Field>
      </form>
      {results.length > 0 && <ul className={styles.accounts}>{results.map((account) => row(account, false))}</ul>}
    </>}
  </Dialog>;
}
