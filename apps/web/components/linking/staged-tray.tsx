"use client";

import {ChevronDown, ChevronUp, Link2, X} from "lucide-react";
import {useMemo, useState} from "react";
import {apiFetch} from "../../lib/api";
import {Button, IconButton} from "../ui/button";
import {providerLabel} from "../ui/primitives";
import styles from "./linking.module.css";
import {errorLabel, type StagedAction, type StagedItem} from "./types";

const CHUNK = 10;
const ACTION_LABEL: Record<StagedAction, string> = {confirm: "关联", reject: "不是同一人", skip: "跳过"};

type Progress = {done: number; total: number; failed: number};

export function StagedTray({staged, unstage, markFailed, clear, onBusy, onApplied}: {
  staged: Record<string, StagedItem>;
  unstage: (accountIds: string[]) => void;
  markFailed: (failures: Record<string, string>) => void;
  clear: () => void;
  onBusy: (busy: boolean) => void;
  onApplied: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [progress, setProgress] = useState<Progress | null>(null);
  const items = useMemo(() => Object.values(staged), [staged]);
  const counts = useMemo(() => {
    const result: Record<StagedAction, number> = {confirm: 0, reject: 0, skip: 0};
    for (const item of items) result[item.action] += 1;
    return result;
  }, [items]);
  const duplicates = useMemo(() => {
    const seen = new Map<string, number>();
    for (const item of items) if (item.action === "confirm") {
      const key = `${item.profile_id}:${item.account_provider}`;
      seen.set(key, (seen.get(key) ?? 0) + 1);
    }
    return new Set([...seen].filter(([, count]) => count > 1).map(([key]) => key));
  }, [items]);
  const busy = progress !== null && progress.done < progress.total;

  if (!items.length && !progress) return null;

  async function apply() {
    const queue = [...items];
    setProgress({done: 0, total: queue.length, failed: 0});
    onBusy(true);
    let failed = 0;
    try {
      for (let start = 0; start < queue.length; start += CHUNK) {
        const chunk = queue.slice(start, start + CHUNK);
        const response = await apiFetch("/api/v1/link-review/batch", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({items: chunk.map(({account_id, action, profile_id, expected_profile_id, score, reasons}) =>
            ({account_id, action, profile_id, expected_profile_id, score, reasons}))}),
        });
        if (!response.ok) {
          const message = response.status === 401 || response.status === 403 ? "登录已失效，请重新登录后再提交" : "服务器出错，未提交";
          markFailed(Object.fromEntries(queue.slice(start).map((item) => [item.account_id, message])));
          failed += queue.length - start;
          break;
        }
        const body = await response.json() as {results: {account_id: string; ok: boolean; error: string | null}[]};
        unstage(body.results.filter((result) => result.ok).map((result) => result.account_id));
        const failures = Object.fromEntries(body.results.filter((result) => !result.ok).map((result) => [result.account_id, errorLabel(result.error)]));
        markFailed(failures);
        failed += Object.keys(failures).length;
        setProgress({done: start + chunk.length, total: queue.length, failed});
      }
    } finally {
      setProgress((value) => value && {...value, done: value.total, failed});
      onBusy(false);
      onApplied();
      if (failed) setOpen(true);
    }
  }

  const summary = (["confirm", "reject", "skip"] as StagedAction[])
    .filter((action) => counts[action])
    .map((action) => `${ACTION_LABEL[action]} ${counts[action]}`)
    .join(" · ");

  return <aside className={styles.tray} aria-label="待提交">
    {open && items.length > 0 && <ul className={styles.trayList}>
      {items.map((item) => <li key={item.account_id} data-error={item.error ? "" : undefined}>
        <span className={styles.trayAction} data-action={item.action}>{ACTION_LABEL[item.action]}</span>
        <span className={styles.trayText}>
          <strong>{providerLabel(item.account_provider)} · {item.account_name}</strong>
          <span>→ {item.profile_name}</span>
          {duplicates.has(`${item.profile_id}:${item.account_provider}`) && <small className={styles.warn}>同一人被选了多个{providerLabel(item.account_provider)}账号，只会成功一个</small>}
          {item.error && <small className={styles.errorText}>{item.error}</small>}
        </span>
        <IconButton size="sm" disabled={busy} label={`移除 ${item.account_name}`} onClick={() => unstage([item.account_id])}><X size={14} /></IconButton>
      </li>)}
    </ul>}
    <div className={styles.trayBar}>
      <Button size="sm" variant="ghost" onClick={() => setOpen((value) => !value)} disabled={!items.length} aria-expanded={open}>
        {open ? <ChevronDown size={14} /> : <ChevronUp size={14} />}已选 {items.length} 项
      </Button>
      <span className={styles.traySummary}>{summary}</span>
      {progress && <div className={styles.progress} role="status">
        <progress max={progress.total} value={progress.done} />
        <span>{busy ? `正在提交 ${progress.done}/${progress.total}` : `完成 ${progress.total - progress.failed}/${progress.total}${progress.failed ? `，${progress.failed} 项失败` : ""}`}</span>
        {!busy && <IconButton size="sm" label="关闭进度" onClick={() => setProgress(null)}><X size={14} /></IconButton>}
      </div>}
      <span className={styles.trayButtons}>
        <Button size="sm" variant="ghost" disabled={busy || !items.length} onClick={clear}>清空</Button>
        <Button size="sm" variant="primary" disabled={busy || !items.length} onClick={() => void apply()}>
          <Link2 size={14} />{busy ? "提交中…" : `统一提交 ${items.length} 项`}
        </Button>
      </span>
    </div>
  </aside>;
}
