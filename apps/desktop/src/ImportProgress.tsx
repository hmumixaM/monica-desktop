import type { DesktopStatus, ImportPhase } from "./types";

const names: Record<string, string> = {
  contacts: "微信联系人", moments: "微信朋友圈", messages: "微信聊天记录",
  wechat_prefetch: "微信图片下载", wechat_media_recovery: "恢复媒体队列",
  wechat_media_ready: "微信媒体导入", wechat_media_incremental: "新增微信媒体",
  instagram: "Instagram 帖子", stories: "Instagram Story", highlights: "Instagram Highlight",
  collector_export: "LinkedIn 资料", projections: "更新关系统计", cache_warmup: "准备浏览数据",
};
const labels: Record<string, string> = {
  running: "正在导入", pending: "等待继续", busy: "等待其他导入完成",
  failed: "需要处理", interrupted: "等待恢复", success: "已完成", unchanged: "没有新内容",
};
const number = (value: number | undefined) => (value ?? 0).toLocaleString("zh-CN");

function Step({ phase, paused }: { phase: ImportPhase; paused: boolean }) {
  const progress = phase.progress;
  const state = paused && ["running", "pending", "busy", "interrupted"].includes(phase.state)
    ? "paused" : phase.state;
  const title = names[phase.phase] ?? phase.phase;
  const detail = progress.messages_inserted !== undefined
    ? `本轮已保存 ${number(progress.messages_inserted)} 条消息`
    : progress.processed !== undefined
      ? `${phase.stage === "fingerprints" ? "已检查" : "已处理"} ${number(progress.processed)}${progress.total !== undefined ? ` / ${number(progress.total)}` : " 条"}`
      : progress.inserted !== undefined ? `本轮新增 ${number(progress.inserted)} 条` : null;
  return (
    <li className="import-step">
      <div className="import-step-heading">
        <strong>{title}</strong>
        <span className={state === "failed" || state === "interrupted" ? "inline-error" : "hint"}>
          {state === "paused" ? "已暂停" : state === "running" && phase.stage === "waiting_io" ? "等待磁盘任务让出资源" : labels[state] ?? "等待状态"}
        </span>
      </div>
      {detail && <p>{detail}</p>}
      {progress.total > 0 && progress.processed !== undefined && (
        <progress aria-label={`${title}处理进度`} max={progress.total} value={Math.min(progress.processed, progress.total)} />
      )}
      {state === "failed" && <p className="hint">后台会重试，可打开日志查看原因。</p>}
      {state === "paused" && <p className="hint">恢复后从已保存的进度继续。</p>}
    </li>
  );
}

export default function ImportProgress({ status }: { status: DesktopStatus }) {
  const phases = status.import_phases ?? [];
  const active = phases.filter((phase) => !["success", "unchanged"].includes(phase.state));
  const completed = phases.filter((phase) => ["success", "unchanged"].includes(phase.state));
  const paused = ["paused", "pausing", "stopped", "stopping"].includes(status.state);
  return (
    <section className="section" aria-labelledby="import-progress-title">
      <div className="section-heading">
        <h2 id="import-progress-title">导入进度</h2>
        <p>内容分批保存，导入期间即可在 Web 中查看已完成的部分。</p>
      </div>
      <dl className="import-counts">
        {[
          ["联系人档案", status.profiles], ["已导入动态", status.posts_imported], ["累计导入消息", status.messages_imported],
        ].map(([label, count]) => <div key={String(label)}><dt>{label}</dt><dd>{number(count as number | undefined)}</dd></div>)}
      </dl>
      {active.length > 0 && <ul className="import-steps">{active.map((phase) => <Step key={`${phase.source}/${phase.phase}`} phase={phase} paused={paused} />)}</ul>}
      {phases.length === 0 && <p className="hint">连接数据源后，这里会显示正在导入的步骤。</p>}
      {completed.length > 0 && <details className="import-history"><summary>已完成的步骤（{completed.length}）</summary><ul className="import-steps">{completed.map((phase) => <Step key={`${phase.source}/${phase.phase}`} phase={phase} paused={paused} />)}</ul></details>}
    </section>
  );
}
