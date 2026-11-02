"use client";

import { type ReactNode, useLayoutEffect, useRef } from "react";

import styles from "./feed.module.css";

/**
 * Shortest-column placement that keeps DOM order for keyboard navigation. A ResizeObserver handles responsive
 * columns, decoded images and changed captions; until measured, CSS supplies an ordinary grid.
 */
export function Masonry({ children, columns }: { children: ReactNode; columns?: "compact" }) {
  const root = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const container = root.current;
    if (!container) return;
    const items = Array.from(container.children) as HTMLElement[];
    let frame = 0;
    let disposed = false;
    const layout = () => {
      frame = 0;
      if (disposed || !container.clientWidth) return;
      const style = getComputedStyle(container);
      const count = Math.max(1, Number(style.getPropertyValue("--feed-columns")) || 2);
      const gap = parseFloat(style.getPropertyValue("--feed-gap")) || 16;
      const width = (container.clientWidth - gap * (count - 1)) / count;
      for (const item of items) item.style.width = `${width}px`;
      const heights = items.map((item) => item.getBoundingClientRect().height);
      const tops = Array(count).fill(0) as number[];
      container.dataset.positioned = "";
      items.forEach((item, index) => {
        const column = tops.indexOf(Math.min(...tops));
        item.style.left = `${column * (width + gap)}px`;
        item.style.top = `${tops[column]}px`;
        tops[column] += heights[index] + gap;
      });
      container.style.height = `${Math.max(0, ...tops) - (items.length ? gap : 0)}px`;
    };
    const schedule = () => { if (!frame && !disposed) frame = requestAnimationFrame(layout); };
    const sizes = new WeakMap<Element, string>();
    const observer = new ResizeObserver((entries) => {
      let changed = false;
      for (const entry of entries) {
        // The container height is an output; only its width can trigger layout.
        const size = entry.target === container ? `${entry.contentRect.width}` : `${entry.contentRect.width}:${entry.contentRect.height}`;
        if (sizes.get(entry.target) !== size) {
          sizes.set(entry.target, size);
          changed = true;
        }
      }
      if (changed) schedule();
    });
    observer.observe(container);
    items.forEach((item) => observer.observe(item));
    layout();
    return () => {
      disposed = true;
      observer.disconnect();
      cancelAnimationFrame(frame);
    };
  }, [children]);
  return <div className={styles.masonryHost}><div className={styles.masonry} data-columns={columns} ref={root}>{children}</div></div>;
}
