import type { DesktopStatus } from "./types";

const lanes: Record<string, string> = { projection: "资料更新", db: "统计与索引", media: "网络图片", ai: "AI 分析" };
const reasons: Record<string, string> = { source_pending: "等待对应内容入库", file_pending: "等待图片文件就绪", unsupported_file: "图片暂不能读取，请查看日志" };
const states: Record<string, string> = { queued: "排队", running: "处理中", failed: "需要重试" };

export default function PipelineProgress({ status }: { status: DesktopStatus }) {
  const data = status.pipeline;
  if (!data) return null;
  const stopped = ["paused", "stopped", "pausing", "stopping"].includes(status.state);
  return <section className="section" aria-labelledby="pipeline-title">
    <div className="section-heading"><h2 id="pipeline-title">任务通道</h2>
      <p>资料更新、图片与分析分别处理。已有内容可先在 Web 中查看。</p></div>
    <p className="hint">待更新资料 {data.projection.count} 批{data.projection.count > 0 && `，最久等待 ${data.projection.oldest_seconds} 秒`}。</p>
    {data.consumers.some(item => item.state === "error") &&
      <p className="inline-error">历史变化记录需要恢复。内容导入继续运行，请打开日志查看原因。</p>}
    {!stopped && data.consumers.some(item => item.heartbeat_age_seconds > 90) &&
      <p className="hint">历史记录稍后补齐，资料导入不受影响。</p>}
    <ul className="import-steps">
      {data.media_waiting.map(item => <li className="import-step" key={item.reason}>
        <div className="import-step-heading"><strong>本地图片关联</strong><span className="hint">{stopped ? "已暂停" : reasons[item.reason] ?? "等待依赖"} · {item.count} 项</span></div>
      </li>)}
      {data.lanes.map(item => <li className="import-step" key={`${item.lane}/${item.status}`}>
        <div className="import-step-heading"><strong>{lanes[item.lane] ?? "后台任务"}</strong>
          <span className={item.status === "failed" ? "inline-error" : "hint"}>{stopped ? "已暂停" : states[item.status] ?? "等待"} · {item.count} 项</span></div>
        <p>已等待 {item.oldest_seconds} 秒{item.retry_waiting > 0 && `，其中 ${item.retry_waiting} 项等待重试时间`}。</p>
      </li>)}
    </ul>
    {data.lanes.length === 0 && data.media_waiting.length === 0 && <p className="hint">当前没有待处理任务。</p>}
  </section>;
}
