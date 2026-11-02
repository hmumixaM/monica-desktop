"use client";

import { type ReactNode, useEffect, useRef, useState } from "react";

/** Mount a section once it approaches the viewport; keep it mounted when scrolling back. */
export function Deferred({ children, height = 120 }: { children: ReactNode; height?: number }) {
  const element = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    if (!element.current || visible) return;
    const observer = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) { setVisible(true); observer.disconnect(); }
    }, { rootMargin: "160px" });
    observer.observe(element.current);
    return () => observer.disconnect();
  }, [visible]);
  return <div ref={element} style={visible ? undefined : { minHeight: height }}>{visible ? children : null}</div>;
}
