"use client";

import { useEffect, useRef } from "react";

import { Button } from "./button";
import styles from "./media.module.css";

/** Automatically continue when the paging boundary approaches the scroll viewport. */
export function LoadMore({ cursor, busy, error, onLoad }: {
  cursor: string; busy: boolean; error?: boolean; onLoad: () => void; label?: string;
}) {
  const boundary = useRef<HTMLDivElement>(null);
  const action = useRef(onLoad);
  action.current = onLoad;
  useEffect(() => {
    if (busy || error || !boundary.current) return;
    let requested = false;
    const observer = new IntersectionObserver((entries) => {
      if (!requested && entries.some((entry) => entry.isIntersecting)) {
        requested = true;
        observer.disconnect();
        action.current();
      }
    }, { rootMargin: "0px 0px 160px 0px" });
    observer.observe(boundary.current);
    return () => observer.disconnect();
  }, [cursor, busy, error]);
  return <div className={styles.loadMore} ref={boundary}>
    <span role="status" aria-live="polite">{error ? "加载失败" : busy ? "正在加载…" : "继续下滑加载更多"}</span>
    {error && <Button variant="ghost" onClick={onLoad}>重试加载</Button>}
  </div>;
}
