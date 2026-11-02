"use client";

import { useEffect, useRef, useState } from "react";

import type { OrbitCommunity } from "../../lib/orbit";
import { Button } from "../ui/button";
import { Checkbox } from "../ui/field";
import { CommunityLegend } from "./community-legend";
import styles from "./graph.module.css";

type GraphRoot = HTMLElement & { _destroyGraph?: () => void; _reloadGraph?: () => Promise<void> };
type GraphCommunity = OrbitCommunity & { color: string };
type GraphWindow = Window & { d3?: unknown; initMonicaGraph?: () => void };

function loadScript(src: string) {
  return new Promise<void>((resolve, reject) => {
    const ready = src.includes("d3.min") ? !!(window as GraphWindow).d3 : !!(window as GraphWindow).initMonicaGraph;
    const existing = document.querySelector(`script[data-mon-suite="${src}"]`);
    if (existing) {
      if (ready) resolve();
      else existing.addEventListener("load", () => resolve(), { once: true });
      return;
    }
    const script = document.createElement("script");
    script.src = src;
    script.async = false;
    script.dataset.monSuite = src;
    script.onload = () => resolve();
    script.onerror = () => reject(new Error(`Unable to load ${src}`));
    document.body.appendChild(script);
  });
}

/**
 * The interaction graph stays a canvas script: it handles thousands of nodes far more predictably than SVG,
 * and already has communities, pan and zoom, search, edge weighting, shared groups and keyboard panning.
 * Markup hooks are the `data-graph-*` attributes the script looks up.
 */
export function GraphView({ focus }: { focus?: string | null }) {
  const rootRef = useRef<GraphRoot>(null);
  const [communities, setCommunities] = useState<GraphCommunity[]>([]);

  useEffect(() => {
    const root = rootRef.current!;
    if (focus) root.dataset.graphSource += `&focus=${encodeURIComponent(focus)}`;
    const listen = (event: Event) => setCommunities((event as CustomEvent<GraphCommunity[]>).detail);
    root.addEventListener("graph-communities", listen);
    void Promise.all([loadScript("/vendor/d3.min.js"), loadScript("/vendor/relationship-graph.js")])
      .then(() => (window as GraphWindow).initMonicaGraph?.())
      .catch(() => {
        const status = root.querySelector("[data-graph-status]");
        if (status) status.textContent = "关系图脚本加载失败，请刷新页面重试。";
      });
    return () => { root.removeEventListener("graph-communities", listen); root._destroyGraph?.(); };
  }, [focus]);

  /** A new name is drawn on the canvas, so the graph is read again. */
  const reload = () => void rootRef.current!._reloadGraph?.();
  const colors = new Map(communities.map((community) => [community.id, community.color]));

  return <section ref={rootRef} className={styles.graph} data-relationship-graph
    data-graph-source="/api/v1/relationships/graph?format=monica" data-contact-url-template="/profiles/__CONTACT_HASH__">
    <div className={styles.bar}>
      <p className={styles.status} role="status" aria-live="polite" data-graph-status>滚动到这里后自动读取关系图。</p>
      <Button size="sm" data-graph-load>加载关系图</Button>
    </div>
    <div className={styles.controls} data-graph-controls hidden>
      <form className={styles.search} role="search" data-graph-search-form>
        <input aria-label="查找节点" type="search" list="relationship-node-options" autoComplete="off" placeholder="查找姓名或微信昵称" data-graph-search />
        <datalist id="relationship-node-options" data-graph-search-options />
        <Button size="sm" type="submit">定位</Button>
      </form>
      <label className={styles.range}>最低边权
        <input type="range" min="0" max="100" step="1" defaultValue="0" data-graph-filter />
        <output data-graph-filter-value>0</output>
      </label>
      <Checkbox data-graph-groups>加入共同群关系</Checkbox>
      <span className={styles.spacer} />
      <Button size="sm" variant="ghost" data-graph-motion aria-pressed="false">节点运动：关</Button>
      <span className={styles.zoom}>
        <Button size="sm" variant="ghost" data-graph-zoom-out aria-label="缩小">−</Button>
        <Button size="sm" variant="ghost" data-graph-zoom-in aria-label="放大">+</Button>
        <Button size="sm" variant="ghost" data-graph-reset>适合全部</Button>
      </span>
    </div>
    <div className={styles.body}>
      <div className={styles.canvasWrap}>
        <canvas className={styles.canvas} data-graph-canvas tabIndex={0} role="img" aria-label="朋友圈互动关系图，可搜索节点，方向键平移，回车打开选中的人" />
      </div>
      <aside className={styles.details} data-graph-details aria-live="polite">加载后，将指针移到节点上查看互动明细。</aside>
    </div>
    <p className={styles.footnote} data-graph-footnote hidden />
    {communities.length > 0 && <details className={styles.communities}>
      <summary>社群名称 · {communities.length}</summary>
      <CommunityLegend communities={communities} colors={colors} onRenamed={reload} />
    </details>}
  </section>;
}
