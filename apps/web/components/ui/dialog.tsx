"use client";

import { X } from "lucide-react";
import { type PointerEvent, type ReactNode, type RefObject, useEffect, useId, useRef } from "react";

import { IconButton } from "./button";
import styles from "./overlay.module.css";

type Size = "default" | "wide" | "full";

function useModal(ref: RefObject<HTMLDialogElement | null>) {
  useEffect(() => {
    const dialog = ref.current;
    if (dialog && !dialog.open) dialog.showModal();
  }, [ref]);
}

/**
 * A modal shown while mounted: unmount it (via `onClose`) to close. Escape, the close button and a click on the
 * backdrop all close it; focus moves in and returns to the opener natively.
 */
export function Dialog({ title, description, onClose, children, size = "default", closeLabel = "关闭", className, actions, dialogRef, locked = false }: {
  title: ReactNode; description?: ReactNode; onClose: () => void; children: ReactNode; size?: Size; closeLabel?: string;
  className?: string; actions?: ReactNode; dialogRef?: RefObject<HTMLDialogElement | null>;
  /** While true (e.g. a save is in flight) Escape, the backdrop and the close button do nothing. */
  locked?: boolean;
}) {
  const own = useRef<HTMLDialogElement>(null);
  const ref = dialogRef ?? own;
  const heading = useId();
  useModal(ref);
  return <dialog ref={ref} className={[styles.dialog, className].filter(Boolean).join(" ")} data-size={size === "default" ? undefined : size} aria-labelledby={heading}
    onClose={onClose} onCancel={(event) => { if (locked) event.preventDefault(); }}
    onClick={(event) => { if (event.target === event.currentTarget && !locked) ref.current?.close(); }}>
    <header className={styles.dialogHeader}>
      <div><h2 id={heading}>{title}</h2>{description && <p>{description}</p>}</div>
      {actions}
      <IconButton label={closeLabel} disabled={locked} onClick={() => ref.current?.close()}><X size={18} /></IconButton>
    </header>
    <div className={styles.dialogBody}>{children}</div>
  </dialog>;
}

const DISMISS_DISTANCE = 96;

/** A bottom sheet on phones (drag the grabber down to dismiss) and a centred panel on wider screens. */
export function Sheet({ label, onClose, children, className }: { label: string; onClose: () => void; children: ReactNode; className?: string }) {
  const ref = useRef<HTMLDialogElement>(null);
  const drag = useRef<{ start: number; offset: number } | null>(null);
  useModal(ref);

  function down(event: PointerEvent<HTMLDivElement>) {
    event.currentTarget.setPointerCapture(event.pointerId);
    drag.current = { start: event.clientY, offset: 0 };
    ref.current!.dataset.dragging = "";
  }
  function move(event: PointerEvent<HTMLDivElement>) {
    if (!drag.current) return;
    drag.current.offset = Math.max(0, event.clientY - drag.current.start);
    ref.current!.style.transform = `translateY(${drag.current.offset}px)`;
  }
  function up() {
    const sheet = ref.current!;
    const offset = drag.current?.offset ?? 0;
    drag.current = null;
    delete sheet.dataset.dragging;
    if (offset > DISMISS_DISTANCE) {
      sheet.animate([{ transform: `translateY(${offset}px)` }, { transform: "translateY(100%)" }], { duration: 160, easing: "ease-in" })
        .finished.then(() => sheet.close());
      sheet.style.transform = "translateY(100%)";
    } else {
      sheet.animate([{ transform: `translateY(${offset}px)` }, { transform: "translateY(0)" }], { duration: 180, easing: "ease-out" });
      sheet.style.transform = "";
    }
  }

  return <dialog ref={ref} aria-label={label} className={[styles.sheet, className].filter(Boolean).join(" ")} onClose={onClose}
    onClick={(event) => { if (event.target === event.currentTarget) ref.current?.close(); }}>
    <div className={styles.grabber} onPointerDown={down} onPointerMove={move} onPointerUp={up} onPointerCancel={up} aria-hidden="true" />
    <div className={styles.sheetBody}>{children}</div>
  </dialog>;
}
