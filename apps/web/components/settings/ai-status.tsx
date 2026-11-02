"use client";

import {Bot, Play} from "lucide-react";

import {type AiJobType, useAiStatus, useRunAiJob, useUpdateImageOCR} from "../../lib/ai";
import {Input} from "../ui/field";
import {Button} from "../ui/button";
import {Failure, formatDateTime, Section, Skeleton} from "../ui/primitives";
import {useToast} from "../ui/toast";
import styles from "./settings.module.css";

const JOBS: Record<AiJobType, {label: string; when: string}> = {
  "ai.community_names": {label: "社群命名", when: "每天 3:00"},
  "ai.reconnect": {label: "该联系了", when: "每天 6:00"},
  "ai.digest": {label: "每周回顾", when: "每周一 8:00"},
  "ai.person_weeks": {label: "每人每周近况", when: "每周一 8:00"},
  "ai.profiles": {label: "人物 Memory 与建议", when: "每周日 1:00"},
  "ai.post_media": {label: "动态与 Story 图片识别", when: "新 Story 异步处理；每小时 :20 补扫"},
};
const STATUS: Record<string, string> = {queued: "排队中", running: "运行中", complete: "已完成", failed: "失败"};

/** Whether the model features are on, the proxy answers, and when each recurring job last ran. */
export function AiStatus() {
  const status = useAiStatus();
  const run = useRunAiJob();
  const toast = useToast();
  const ocr = useUpdateImageOCR();
  const data = status.data;
  const start = (job: AiJobType) => run.mutate(job, {
    onSuccess: () => toast.show({message: `已排队：${JOBS[job].label}`}),
    onError: () => toast.show({message: "没能排队，请重试"}),
  });
  return <Section title="模型服务" className={styles.panel}>
    {status.isError ? <Failure onRetry={() => void status.refetch()}>读不到模型服务状态。</Failure>
      : !data ? <Skeleton height={80} />
        : !data.enabled ? <div className={styles.row}>
          <span className={styles.icon}><Bot size={18} /></span>
          <div className={styles.text}><strong>未开启</strong><small>在 NAS 的 .env 里设置 AI_ENABLED=true 后，社群命名、该联系了、每周回顾和资料建议会出现。</small></div>
        </div>
          : <ul className={styles.list}>
            <li className={styles.row}>
              <span className={styles.icon} data-on={data.reachable ? "" : undefined}><Bot size={18} /></span>
              <div className={styles.text}>
                <strong>{data.reachable ? "LiteLLM 可用" : "LiteLLM 无响应"}</strong>
                <small>批量任务 {data.models?.batch} · 人物近况 {data.models?.person} · 写作 {data.models?.writer} · 识图 {data.models?.vision}。人物 Memory 和每周近况读取 Monica 已存的相关私聊、本人群聊发言、动态、资料及动态图片的识别文字。{data.image_ocr?.include_chat ? "历史 OCR 补扫也处理聊天图片；聊天图片 OCR 不加入人物分析。" : "聊天图片不发送。"}</small>
              </div>
            </li>
            {data.image_ocr && <li className={styles.row}>
              <span />
              <div className={styles.text}>
                <strong>历史图片 OCR（从新到旧） · {data.image_ocr.enabled ? "已启用" : "已暂停"}</strong>
                <small>{data.image_ocr.model} · {data.image_ocr.include_chat ? "帖子、Story 和聊天图片，跳过头像" : "帖子与 Story 图片"}</small>
                <small>完成 {(data.image_ocr.counts.complete ?? 0).toLocaleString()} 张 · 待处理 {(data.image_ocr.counts.pending ?? 0).toLocaleString()} 张 · 失败 {data.image_ocr.counts.failed ?? 0} 张</small>
                <small>今日已开始 {data.image_ocr.attempted_today.toLocaleString()} / {data.image_ocr.daily_limit.toLocaleString()} 张（重试也计入） · 按限额约剩 {data.image_ocr.estimated_days_remaining} 天</small>
                <form onSubmit={(event) => {
                  event.preventDefault();
                  const daily_limit = Number(new FormData(event.currentTarget).get("daily_limit"));
                  ocr.mutate({daily_limit}, {onSuccess: () => toast.show({message: "每日 OCR 限额已保存"}), onError: () => toast.show({message: "保存失败，请重试"})});
                }}>
                  <label>每天最多处理图片数 <Input key={data.image_ocr.daily_limit} name="daily_limit" type="number" min={1} max={100000} required defaultValue={data.image_ocr.daily_limit} style={{width: 120, display: "inline-block"}} /></label>{" "}
                  <Button size="sm" variant="ghost" disabled={ocr.isPending}>保存</Button>
                </form>
              </div>
              <Button size="sm" variant="ghost" disabled={ocr.isPending} onClick={() => ocr.mutate({enabled: !data.image_ocr!.enabled})}>{data.image_ocr.enabled ? "暂停" : "继续"}</Button>
            </li>}
            {data.jobs?.map((job) => <li key={job.job_type} className={styles.row}>
              <span />
              <div className={styles.text}>
                <strong>{JOBS[job.job_type].label}<small className={styles.inline}> · {JOBS[job.job_type].when}</small></strong>
                <small>{job.status ? `${STATUS[job.status] ?? job.status}${job.completed_at ? ` · ${formatDateTime(job.completed_at, true)}` : ""}` : "还没运行过"}
                  {job.status === "failed" && job.error ? ` · ${job.error.slice(0, 120)}` : ""}</small>
              </div>
              <Button size="sm" variant="ghost" onClick={() => start(job.job_type)} disabled={run.isPending || !data.reachable}><Play size={13} />运行</Button>
            </li>)}
          </ul>}
  </Section>;
}
