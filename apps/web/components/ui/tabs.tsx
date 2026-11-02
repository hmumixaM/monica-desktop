import type { ReactNode } from "react";

import styles from "./controls.module.css";

export interface TabItem { id: string; label: ReactNode }

/** Section tabs marked with `aria-current`, so their ids stay addressable as URL state. */
export function Tabs({ items, value, onSelect, label, className }: {
  items: TabItem[]; value: string; onSelect: (id: string) => void; label: string; className?: string;
}) {
  return <nav className={[styles.tabs, className].filter(Boolean).join(" ")} aria-label={label}>
    {items.map((item) => <button key={item.id} type="button" aria-current={item.id === value ? "page" : undefined}
      onClick={() => onSelect(item.id)}>{item.label}</button>)}
  </nav>;
}
