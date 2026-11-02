"use client";

import { type PointerEvent, useLayoutEffect, useRef, useState } from "react";

import { LETTERS } from "../../lib/letters";
import styles from "./contacts.module.css";

/**
 * The A–Z strip beside the list. Tap a letter, or drag along the strip and a bubble shows where you are;
 * letters with no one under them are dimmed and jump to the next letter that has someone.
 */
export function LetterIndex({ present, onJump }: { present: Set<string>; onJump: (letter: string) => void }) {
  const strip = useRef<HTMLDivElement>(null);
  const [dragging, setDragging] = useState<string | null>(null);
  const last = useRef<string | null>(null);

  useLayoutEffect(() => {
    const element = strip.current!;
    let frame = 0;
    const measure = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        element.style.setProperty("--index-top", `${Math.max(0, element.getBoundingClientRect().top)}px`);
      });
    };
    // The toolbar can wrap or expand; reserve the actual remaining viewport.
    element.style.setProperty("--index-top", `${Math.max(0, element.getBoundingClientRect().top)}px`);
    const observer = new ResizeObserver(measure);
    observer.observe(document.documentElement);
    window.addEventListener("scroll", measure, { passive: true });
    window.addEventListener("resize", measure);
    window.visualViewport?.addEventListener("resize", measure);
    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
      window.removeEventListener("scroll", measure);
      window.removeEventListener("resize", measure);
      window.visualViewport?.removeEventListener("resize", measure);
    };
  }, []);

  function letterAt(event: PointerEvent) {
    const box = strip.current!.getBoundingClientRect();
    const position = Math.max(0, Math.min(LETTERS.length - 1, Math.floor(((event.clientY - box.top) / box.height) * LETTERS.length)));
    return LETTERS[position];
  }
  function jump(letter: string) {
    if (letter === last.current) return;
    last.current = letter;
    setDragging(letter);
    onJump(letter);
  }

  return <div ref={strip} className={styles.index} role="navigation" aria-label="按首字母跳转"
    onPointerDown={(event) => {
      event.currentTarget.setPointerCapture(event.pointerId);
      last.current = null;
      jump(letterAt(event));
    }}
    onPointerMove={(event) => { if (event.currentTarget.hasPointerCapture(event.pointerId)) jump(letterAt(event)); }}
    onPointerUp={() => setDragging(null)} onPointerCancel={() => setDragging(null)}>
    {LETTERS.map((letter) => <button key={letter} type="button" tabIndex={-1} data-empty={present.has(letter) ? undefined : ""}
      aria-label={`跳到 ${letter}`}>{letter}</button>)}
    {dragging && <span className={styles.bubble} aria-hidden="true"
      style={{ insetBlockStart: `${((LETTERS.indexOf(dragging) + 0.5) / LETTERS.length) * 100}%` }}>{dragging}</span>}
  </div>;
}
