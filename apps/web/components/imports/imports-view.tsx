"use client";

import {RefreshCw} from "lucide-react";
import {useCallback, useEffect, useState} from "react";
import {apiFetch} from "../../lib/api";
import {Button} from "../ui/button";
import {Dialog} from "../ui/dialog";
import {Failure, formatDateTime, formatNumber, Loading, Page, PageHeader, providerLabel, Section} from "../ui/primitives";
import {CollectorTokens} from "./collector-tokens";
import {IdentityReview} from "./identity-review";
import {RawData} from "./raw-data";
import {HistoryChanges} from "./history-changes";
import styles from "./imports.module.css";

type Batch = {
  id: string; batch_id: string; source: string; stream: string; status: string; received_at: string; completed_at?: string;
  inserted_count: number; changed_count: number; unchanged_count: number; error_summary?: string;
};
type Audit = {
  sources: {source: string; stream: string; source_count: number; projected_count: number; excluded_count: number; stage: string; checked_at: string; details: unknown; missing_ids: unknown}[];
  media: {state: string; count: number}[];
  jobs: {job_type: string; status: string; count: number}[];
  messages: {processed_tables: number; source_rows: number; processed_rows: number};
};

const STATES: Record<string, string> = {
  ready: "本地可用", pending: "等待下载", downloading: "下载中", expired: "来源已过期", unavailable: "来源不可用",
  failed: "处理失败", projected: "对象已投影", reconciled: "已对账",
};
const label = (state: string) => STATES[state] ?? state;

export function ImportsView() {
  const [batches, setBatches] = useState<Batch[]>([]);
  const [audit, setAudit] = useState<Audit | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<Batch | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const responses = await Promise.all([fetch("/api/v1/imports/batches"), fetch("/api/v1/imports/reconciliation")]);
      if (responses.some((response) => !response.ok)) throw new Error("imports");
      setBatches(await responses[0].json());
      setAudit(await responses[1].json());
      setError("");
    } catch {
      setError("暂时无法读取导入记录，请重试。");
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {void load();}, [load]);

  return <Page width="wide">
    <PageHeader title="导入中心"
      actions={<Button size="sm" onClick={() => void load()} disabled={loading}><RefreshCw size={14} />刷新</Button>} />
    {error && <Failure onRetry={() => void load()}>{error}</Failure>}
    <IdentityReview />
    <HistoryChanges />

    <Section title="来源对账" className={styles.panel}>
      {audit && (audit.sources.length ? <ul className={styles.sources}>
        {audit.sources.map((source) => <li key={`${source.source}:${source.stream}`}>
          <div className={styles.sourceHead}>
            <strong>{providerLabel(source.source)} · {source.stream}</strong>
            <span className={styles.stage} data-stage={source.stage}>{label(source.stage)}</span>
          </div>
          <dl className={styles.counts}>
            <div><dt>源对象</dt><dd>{formatNumber(source.source_count)}</dd></div>
            <div><dt>已投影</dt><dd>{formatNumber(source.projected_count)}</dd></div>
            <div><dt>排除</dt><dd>{formatNumber(source.excluded_count)}</dd></div>
            <div><dt>核对时间</dt><dd>{formatDateTime(source.checked_at)}</dd></div>
          </dl>
          <RawData value={{details: source.details, missing: source.missing_ids}} />
        </li>)}
      </ul> : <p className={styles.none}>尚无完整的来源对账记录。</p>)}
      {audit && <p className={styles.note}>
        消息已核对 {audit.messages.processed_tables} 个源表，游标处理 {formatNumber(audit.messages.processed_rows)} / {formatNumber(audit.messages.source_rows)} 行。此分母只含已扫描的源表。
      </p>}
      {!audit && loading && <Loading />}
    </Section>

    {audit?.media.length ? <Section title="媒体归档" className={styles.panel}>
      <dl className={styles.media}>
        {audit.media.map((media) => <div key={media.state} data-state={media.state}><dt>{label(media.state)}</dt><dd>{formatNumber(media.count)}<small> 个来源引用</small></dd></div>)}
      </dl>
    </Section> : null}

    <Section title="最近批次" className={styles.panel} action={<span className={styles.note}>未变化的内容只增加观察记录</span>}>
      {loading && !batches.length ? <Loading /> : <ul className={styles.batches}>
        {batches.map((batch) => <li key={batch.id}>
          <div>
            <strong>{providerLabel(batch.source)} · {batch.stream}</strong>
            <small>{formatNumber(batch.inserted_count)} 新增 · {formatNumber(batch.changed_count)} 变化 · {formatNumber(batch.unchanged_count)} 未变化</small>
            {batch.error_summary && <small className={styles.failed}>{batch.error_summary}</small>}
          </div>
          <span className={styles.stage} data-stage={batch.status}>{batch.status === "complete" ? "已处理" : batch.status}</span>
          <time>{formatDateTime(batch.completed_at || batch.received_at)}</time>
          <Button size="sm" variant="ghost" onClick={() => setSelected(batch)}>查看</Button>
        </li>)}
      </ul>}
    </Section>

    <CollectorTokens />

    {selected && <BatchAudit batch={selected} onClose={() => setSelected(null)} onReplayed={load} />}
  </Page>;
}

function BatchAudit({batch, onClose, onReplayed}: {batch: Batch; onClose: () => void; onReplayed: () => Promise<void>}) {
  const [detail, setDetail] = useState<unknown>(null);
  const [replaying, setReplaying] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void fetch(`/api/v1/imports/batches/${encodeURIComponent(batch.batch_id)}`).then(async (response) => {
      const body = response.ok ? await response.json() : {error: "无法读取批次"};
      if (!cancelled) setDetail(body);
    });
    return () => {cancelled = true;};
  }, [batch.batch_id]);

  async function replay() {
    setReplaying(true);
    try {
      const response = await apiFetch(`/api/v1/imports/batches/${encodeURIComponent(batch.batch_id)}/replay`, {method: "POST"});
      setDetail(await response.json());
      await onReplayed();
    } finally {
      setReplaying(false);
    }
  }

  return <Dialog title="批次审计" description={`${providerLabel(batch.source)} · ${batch.stream} · ${batch.batch_id}`} size="wide" onClose={onClose} locked={replaying}>
    <dl className={styles.counts}>
      <div><dt>新增</dt><dd>{formatNumber(batch.inserted_count)}</dd></div>
      <div><dt>变化</dt><dd>{formatNumber(batch.changed_count)}</dd></div>
      <div><dt>未变化</dt><dd>{formatNumber(batch.unchanged_count)}</dd></div>
      <div><dt>接收时间</dt><dd>{formatDateTime(batch.received_at)}</dd></div>
    </dl>
    {detail ? <RawData value={detail} open /> : <Loading />}
    <div className={styles.dialogActions}>
      <Button disabled={replaying || !detail} onClick={() => void replay()}>{replaying ? "重放中…" : "幂等重放此批次"}</Button>
    </div>
  </Dialog>;
}
