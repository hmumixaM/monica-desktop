"use client";

import {useQuery} from "@tanstack/react-query";
import {fetchJson} from "../../lib/queries";
import {Failure, formatDateTime, Section, Skeleton} from "../ui/primitives";
import styles from "./settings.module.css";

type Health = {
  queues: {job_type: string; resource_class: string; status: string; count: number; oldest_due_at: string | null; oldest_heartbeat_at: string | null; last_success_at: string | null; expired_leases: number}[];
  workers: {worker_id: string; resource_class: string; heartbeat_at: string; draining: boolean; stale: boolean}[];
  sources: {source: string; phase: string; status: string; stale: boolean; last_success_at: string | null; next_retry_at: string | null; failures: number; error: string | null; details: {lag_bytes?: number}}[];
};
const labels: Record<string, string> = {queued: "等待", running: "执行中", complete: "完成", failed: "失败", success: "成功", unchanged: "没有新数据", busy: "等待来源锁", pending: "还有待处理数据"};
const stamp = (value: string | null) => value ? formatDateTime(value, true) : "尚无记录";

export function TaskStatus() {
  const query = useQuery({queryKey: ["task-health"], queryFn: () => fetchJson<Health>("/api/v1/jobs/status"), refetchInterval: 30_000});
  return <Section title="后台任务与来源" className={styles.panel}>
    <p>调度完成表示任务已分发；实际处理进度以各任务队列为准。每 30 秒更新。</p>
    {query.isError ? <Failure onRetry={() => void query.refetch()}>无法读取后台状态。</Failure>
      : !query.data ? <Skeleton height={80} /> : <>
        <ul className={styles.list}>{query.data.queues.map(row => <li className={styles.row} key={`${row.job_type}-${row.status}`}>
          <div className={styles.text}><strong>{row.job_type} · {labels[row.status] ?? row.status} {row.count}</strong>
            <small>{row.oldest_due_at ? `最早等待：${stamp(row.oldest_due_at)}` : row.last_success_at ? `最近完成：${stamp(row.last_success_at)}` : ""}
              {row.oldest_heartbeat_at ? ` · 最早心跳：${stamp(row.oldest_heartbeat_at)}` : ""}
              {row.expired_leases > 0 ? ` · ${row.expired_leases} 个租约过期，等待接管` : ""}</small></div>
        </li>)}</ul>
        <ul className={styles.list}>{query.data.workers.map(row => <li className={styles.row} key={row.worker_id}>
          <div className={styles.text}><strong>{row.resource_class} · {row.stale ? "心跳过期" : row.draining ? "正在收尾" : "运行正常"}</strong>
            <small>{row.worker_id} · {stamp(row.heartbeat_at)}</small></div>
        </li>)}</ul>
        <ul className={styles.list}>{query.data.sources.map(row => <li className={styles.row} key={`${row.source}-${row.phase}`}>
          <div className={styles.text}><strong>{row.source} / {row.phase} · {row.stale ? "心跳过期" : labels[row.status] ?? row.status}</strong>
            <small>最近成功：{stamp(row.last_success_at)}{row.failures ? ` · 连续失败 ${row.failures} 次` : ""}
              {row.next_retry_at ? ` · 下次重试：${stamp(row.next_retry_at)}` : ""}
              {row.details.lag_bytes ? ` · 待消费 ${row.details.lag_bytes.toLocaleString()} 字节` : ""}
              {row.error ? ` · ${row.error.slice(0, 160)}` : ""}</small></div>
        </li>)}</ul>
      </>}
  </Section>;
}
