import { X } from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";

import styles from "./controls.module.css";

type Common = { children: ReactNode; count?: number; active?: boolean; title?: string; className?: string };

function Body({ children, count }: { children: ReactNode; count?: number }) {
  return <><span>{children}</span>{count != null && <b>{count.toLocaleString("zh-CN")}</b>}</>;
}

export function Chip({ children, count, active, title, className, onRemove, removeLabel }: Common & {
  onRemove?: () => void; removeLabel?: string;
}) {
  return <span className={[styles.chip, className].filter(Boolean).join(" ")} data-active={active ? "" : undefined} title={title}>
    <Body count={count}>{children}</Body>
    {onRemove && <button type="button" className={styles.remove} aria-label={removeLabel ?? "移除"} onClick={onRemove}><X size={11} /></button>}
  </span>;
}

export function ChipButton({ children, count, active, title, className, onClick, disabled }: Common & {
  onClick: () => void; disabled?: boolean;
}) {
  return <button type="button" className={[styles.chip, className].filter(Boolean).join(" ")} aria-pressed={active ?? undefined}
    title={title} onClick={onClick} disabled={disabled}><Body count={count}>{children}</Body></button>;
}

export function ChipLink({ children, count, active, title, className, href }: Common & { href: string }) {
  return <Link href={href} className={[styles.chip, className].filter(Boolean).join(" ")} data-active={active ? "" : undefined}
    title={title}><Body count={count}>{children}</Body></Link>;
}
