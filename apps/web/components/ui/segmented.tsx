import Link from "next/link";
import type { ReactNode } from "react";

import styles from "./controls.module.css";

export interface SegmentOption<T extends string> { value: T; label: ReactNode; title?: string; count?: number; disabled?: boolean }

/** One choice out of a few, as pressed buttons. */
export function Segmented<T extends string>({ options, value, onChange, label, size, className }: {
  options: SegmentOption<T>[]; value: T; onChange: (value: T) => void; label: string; size?: "sm"; className?: string;
}) {
  return <div className={[styles.segmented, className].filter(Boolean).join(" ")} role="group" aria-label={label} data-size={size}>
    {options.map((option) => <button key={option.value} type="button" aria-pressed={option.value === value} title={option.title}
      disabled={option.disabled} onClick={() => onChange(option.value)}>
      {option.label}{option.count != null && <b>{option.count.toLocaleString("zh-CN")}</b>}
    </button>)}
  </div>;
}

/** The same control when each option is its own URL. */
export function SegmentedLinks({ options, label, size, className }: {
  options: { href: string; label: ReactNode; active: boolean }[]; label: string; size?: "sm"; className?: string;
}) {
  return <nav className={[styles.segmented, className].filter(Boolean).join(" ")} aria-label={label} data-size={size}>
    {options.map((option) => <Link key={option.href} href={option.href} aria-current={option.active ? "page" : undefined} scroll={false}>{option.label}</Link>)}
  </nav>;
}
