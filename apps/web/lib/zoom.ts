"use client";

import { type RefObject, useCallback, useEffect } from "react";

const EASE = "cubic-bezier(.2, .8, .2, 1)";

export function motionAllowed() {
  return typeof window !== "undefined" && !matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function offset(from: DOMRect, to: DOMRect) {
  const scale = from.width / Math.max(1, to.width);
  const dx = from.left + from.width / 2 - (to.left + to.width / 2);
  const dy = from.top + from.height / 2 - (to.top + to.height / 2);
  return `translate(${dx}px, ${dy}px) scale(${scale})`;
}

/**
 * Photo zoom for a modal opened from a thumbnail: the dialog's `[data-zoom-target]` grows out of `origin`
 * when shown and shrinks back into it on close. Returns the close function to use instead of `dialog.close()`.
 */
export function useZoomFrom(dialog: RefObject<HTMLDialogElement | null>, origin: HTMLElement | null | undefined, ready = true) {
  useEffect(() => {
    const element = dialog.current;
    if (!element || !origin || !ready || !motionAllowed()) return;
    const frame = requestAnimationFrame(() => {
      const target = element.querySelector<HTMLElement>("[data-zoom-target]");
      if (!target) return;
      const to = target.getBoundingClientRect();
      if (!to.width) return;
      target.animate([{ transform: offset(origin.getBoundingClientRect(), to) }, { transform: "none" }], { duration: 340, easing: EASE });
    });
    return () => cancelAnimationFrame(frame);
  }, [dialog, origin, ready]);

  return useCallback(() => {
    const element = dialog.current;
    if (!element) return;
    const target = element.querySelector<HTMLElement>("[data-zoom-target]");
    if (!origin?.isConnected || !target || !motionAllowed()) {
      element.close();
      return;
    }
    const to = target.getBoundingClientRect();
    target.animate([{ transform: "none" }, { transform: offset(origin.getBoundingClientRect(), to) }], { duration: 240, easing: EASE, fill: "forwards" });
    element.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 240, easing: "ease-in", fill: "forwards" })
      .finished.then(() => element.close());
  }, [dialog, origin]);
}
