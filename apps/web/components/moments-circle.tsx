"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import { loadScript } from "../lib/scripts";
import { useResolvedTheme } from "../lib/theme";
import styles from "./moments-circle.module.css";
import { Button } from "./ui/button";
import { Failure, Loading, Panel } from "./ui/primitives";

type Node = {
  id: string; label: string; url: string; x: number; y: number; out: number[]; in: number[]; direct: number; weight: number; group: number;
  groupOnly: boolean; reciprocity: number; like: number; comment: number; reply: number; mention: number; last?: string;
};
type Graph = { center: { id: string; label: string; url: string }; neighbors: Node[]; maximumDirect: number; maximumWeight: number };
type GraphRoot = HTMLDivElement & { _destroyGraph?: () => void };

const LEGEND = [["reciprocal", "互惠"], ["outward", "本人较主动"], ["inward", "对方较主动"]] as const;

/** Moments interactions centred on one person, drawn by the vendor D3 script; it repaints when the theme changes. */
export function MomentsCircle({ profileId }: { profileId: string }) {
  const root = useRef<GraphRoot>(null);
  const theme = useResolvedTheme();
  const [graph, setGraph] = useState<Graph | null>(null);
  const [error, setError] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const abort = new AbortController();
    setGraph(null);
    setError(false);
    const read = () => fetch(`/api/v1/profiles/${profileId}/moments-circle`, { signal: abort.signal }).then((response) => {
      if (!response.ok) throw new Error(String(response.status));
      return response.json();
    });
    // A busy NAS can time out one cold computation; retry once before asking.
    read().catch((reason: Error) => {
      if (reason.name === "AbortError") throw reason;
      return new Promise((resolve) => setTimeout(resolve, 1500)).then(read);
    }).then(setGraph).catch((reason: Error) => { if (reason.name !== "AbortError") setError(true); });
    return () => abort.abort();
  }, [profileId, attempt]);
  useEffect(() => {
    if (!graph) return;
    let cancelled = false;
    const element = root.current;
    void loadScript("/vendor/d3.min.js").then(() => loadScript("/vendor/moments-circle.js")).then(() => {
      if (!cancelled) document.dispatchEvent(new CustomEvent("wechat:lazy-loaded", { detail: { root: element } }));
    });
    return () => {
      cancelled = true;
      element?._destroyGraph?.();
      if (element) {
        delete element.dataset.momentsD3Ready;
        element.querySelector("svg")?.replaceChildren();
      }
    };
  }, [graph, theme]);
  const direct = graph?.neighbors.filter((node) => !node.groupOnly) ?? [];
  return <Panel className={styles.circle}>
    {/* Remounting per theme gives the vendor script fresh controls, so listeners are never bound twice. */}
    <div key={theme} ref={root} data-moments-d3-graph={graph ? "" : undefined} className={styles.root}>
      <header className={styles.header}>
        <div><h2>朋友圈互动</h2>{graph && <small>{direct.length} 位互动对象 · 强关系靠近中心</small>}</div>
        <Link href={`/relationships?focus=${profileId}`}>展开关系网络</Link>
      </header>
      {error ? <Failure onRetry={() => setAttempt((value) => value + 1)}>互动图暂时没有读取成功。</Failure> : !graph ? <Loading>正在读取朋友圈互动…</Loading> : <>
        <div className={styles.counts}>{["点赞", "评论", "回复", "提醒"].map((label, index) => <span key={label}>
          <b>{direct.reduce((sum, node) => sum + node.out[index] + node.in[index], 0).toLocaleString("zh-CN")}</b>{label}
        </span>)}</div>
        <div className={styles.controls}>
          <label><input type="checkbox" data-moments-group-toggle />共同群</label>
          <label>互动下限 <input type="range" min="0" max="100" defaultValue="0" data-moments-filter /><output data-moments-filter-value>0</output></label>
          <span className={styles.buttons}>
            <Button size="sm" variant="ghost" data-moments-zoom-in>放大</Button>
            <Button size="sm" variant="ghost" data-moments-zoom-out>缩小</Button>
            <Button size="sm" variant="ghost" data-moments-reset>重置</Button>
            <Button size="sm" data-moments-motion aria-pressed="true">暂停运动</Button>
          </span>
        </div>
        <svg data-moments-graph-svg className={styles.canvas} role="img" aria-label="以当前人为中心的朋友圈互动图，节点可点击打开联系人" />
        <p className={styles.legend}>{LEGEND.map(([key, label]) => <span key={key} data-kind={key}>{label}</span>)}<span data-kind="group">虚线：共同群（80 人以内）</span></p>
        <p className={styles.details} data-moments-graph-details aria-live="polite" />
        <script type="application/json" data-moments-graph-data dangerouslySetInnerHTML={{ __html: JSON.stringify(graph).replace(/</g, "\\u003c") }} />
        <details className={styles.roster}>
          <summary>互动明细与联系人（<span data-moments-visible-count>{direct.length}</span>）</summary>
          <div>{direct.map((node) => <Link href={node.url} key={node.id}><strong>{node.label}</strong><span>赞 {node.like} · 评论 {node.comment} · 回复 {node.reply}</span></Link>)}</div>
        </details>
      </>}
    </div>
  </Panel>;
}
