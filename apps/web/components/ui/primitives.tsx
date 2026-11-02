"use client";

import { Instagram, Linkedin, MessageCircle, NotebookPen } from "lucide-react";
import type { CSSProperties, ReactNode } from "react";

import type { MonthCount } from "../../lib/api-types";
import { localMediaUrl } from "../../lib/media";
import { Button } from "./button";
import styles from "./primitives.module.css";

export const PROVIDER_LABEL: Record<string, string> = {
  wechat: "微信", wechat_group: "微信群", instagram: "Instagram", linkedin: "LinkedIn", monica: "Monica", manual: "手工",
};

export function providerLabel(provider: string) {
  return PROVIDER_LABEL[provider] ?? provider;
}

/** `decorative` when the provider name is already written beside the mark, so it is not read twice. */
export function SourceMark({ provider, size = 14, decorative = false }: { provider: string; size?: number; decorative?: boolean }) {
  const Icon = provider === "instagram" ? Instagram : provider === "linkedin" ? Linkedin
    : provider === "monica" || provider === "manual" ? NotebookPen : MessageCircle;
  return <span className={styles.source} data-provider={provider} title={decorative ? undefined : providerLabel(provider)}
    aria-hidden={decorative || undefined} style={{ "--mark-size": `${size}px` } as CSSProperties}>
    <Icon size={size} aria-hidden="true" />{!decorative && <span className={styles.srOnly}>{providerLabel(provider)}</span>}
  </span>;
}

export function Avatar({ name, mediaId, size = 40, ring, round, transitionName }: {
  name: string; mediaId?: string | null; size?: number;
  /** Source shares drawn as a ring around the avatar, e.g. { wechat: 3, linkedin: 1 }. */
  ring?: Record<string, number>;
  round?: boolean;
  /** View-transition name, so this avatar morphs into the same person's avatar on the next page. */
  transitionName?: string;
}) {
  const initials = [...name.trim()].slice(0, name.match(/^[\p{Script=Han}]/u) ? 1 : 2).join("").toUpperCase();
  const src = localMediaUrl(mediaId);
  const style = {
    "--avatar-size": `${size}px`, ...(ring ? { "--ring": ringGradient(ring) } : {}),
    ...(transitionName ? { viewTransitionName: transitionName } : {}),
  } as CSSProperties;
  return <span className={styles.avatar} data-avatar="" data-ring={ring ? "" : undefined} data-round={round ? "" : undefined} style={style}>
    {src ? <img src={src} alt="" width={size} height={size} loading="lazy" decoding="async" /> : <span aria-hidden="true">{initials || "?"}</span>}
  </span>;
}

function ringGradient(shares: Record<string, number>) {
  const entries = Object.entries(shares).filter(([, value]) => value > 0);
  const total = entries.reduce((sum, [, value]) => sum + value, 0) || 1;
  let start = 0;
  const stops = entries.map(([provider, value]) => {
    const end = start + (value / total) * 360;
    const stop = `var(--source-${provider}, var(--color-text-faint)) ${start}deg ${end}deg`;
    start = end;
    return stop;
  });
  return `conic-gradient(${stops.join(", ") || "var(--color-text-faint) 0deg 360deg"})`;
}

/** The page frame: one width, consistent gutters. */
export function Page({ children, width, className, label }: {
  children: ReactNode; width?: "narrow" | "wide" | "full"; className?: string; label?: string;
}) {
  return <div className={[styles.page, className].filter(Boolean).join(" ")} data-width={width} aria-label={label}>{children}</div>;
}

export function PageHeader({ title, count, description, actions }: { title: ReactNode; count?: ReactNode; description?: ReactNode; actions?: ReactNode }) {
  return <header className={styles.pageHeader}>
    <div><h1>{title}{count != null && count !== "" && <small>{count}</small>}</h1>{description && <p>{description}</p>}</div>
    {actions && <div className={styles.pageActions}>{actions}</div>}
  </header>;
}

function isEmpty(children: ReactNode) {
  return children === null || children === undefined || children === false || (Array.isArray(children) && !children.some(Boolean));
}

/** Renders nothing when there is nothing to show: empty blocks never take space. */
export function Section({ title, action, children, className, panel, id, icon }: {
  title: ReactNode; action?: ReactNode; children?: ReactNode; className?: string; panel?: boolean; id?: string; icon?: ReactNode;
}) {
  if (isEmpty(children)) return null;
  return <section id={id} className={[styles.section, panel && styles.panel, className].filter(Boolean).join(" ")}>
    <header><h2>{icon}{title}</h2>{action}</header>
    {children}
  </section>;
}

export function Panel({ children, className, flush, as: Tag = "section", label }: {
  children: ReactNode; className?: string; flush?: boolean; as?: "section" | "div" | "aside"; label?: string;
}) {
  return <Tag className={[styles.panel, className].filter(Boolean).join(" ")} data-flush={flush ? "" : undefined} aria-label={label}>{children}</Tag>;
}

export function Stat({ value, label, highlight, size, title }: {
  value: ReactNode; label: string; highlight?: boolean; size?: "sm"; title?: string;
}) {
  return <div className={styles.stat} data-highlight={highlight ? "" : undefined} data-size={size} title={title}>
    <strong>{value}</strong><span>{label}</span>
  </div>;
}

/** Monthly contact intensity: one bar per month, the peak marked with the highlighter. */
export function PulseStrip({ months, compact = false, label }: { months: MonthCount[]; compact?: boolean; label: string }) {
  const totals = months.map((month) => month.sent + month.received);
  const peak = Math.max(...totals, 1);
  return <ol className={styles.pulse} data-compact={compact ? "" : undefined} aria-label={label}>
    {months.map((month, index) => {
      const total = totals[index];
      const level = total ? Math.max(0.12, total / peak) : 0;
      return <li key={month.month} style={{ "--level": level } as CSSProperties}
        data-peak={total === peak && total > 0 ? "" : undefined}
        title={`${month.month}：${total.toLocaleString("zh-CN")} 条（我 ${month.sent} · 对方 ${month.received}）`}>
        <span className={styles.srOnly}>{month.month} {total} 条</span>
      </li>;
    })}
  </ol>;
}

export function EmptyState({ icon, title, children, actions }: { icon?: ReactNode; title: ReactNode; children?: ReactNode; actions?: ReactNode }) {
  return <div className={styles.empty}>{icon}<strong>{title}</strong>{children && <span>{children}</span>}{actions && <div>{actions}</div>}</div>;
}

export function Skeleton({ height = 120, className, style }: { height?: number | string; className?: string; style?: CSSProperties }) {
  return <span className={[styles.skeleton, className].filter(Boolean).join(" ")} style={{ blockSize: height, ...style }} aria-hidden="true" />;
}

/** A loading line for polite status. */
export function Loading({ children = "正在读取…" }: { children?: ReactNode }) {
  return <p className={styles.inlineState} role="status">{children}</p>;
}

/** A failure line with a retry button next to it. */
export function Failure({ children, onRetry, retryLabel = "重试" }: { children: ReactNode; onRetry?: () => void; retryLabel?: string }) {
  return <p className={styles.inlineState} role="alert">{children}{onRetry && <Button size="sm" variant="secondary" onClick={onRetry}>{retryLabel}</Button>}</p>;
}

export function Muted({ children, className }: { children: ReactNode; className?: string }) {
  return <p className={[styles.muted, className].filter(Boolean).join(" ")}>{children}</p>;
}

export function Faint({ children, className }: { children: ReactNode; className?: string }) {
  return <p className={[styles.faint, className].filter(Boolean).join(" ")}>{children}</p>;
}

export function relativeDays(value?: string | null): string | null {
  if (!value) return null;
  const days = Math.floor((Date.now() - new Date(value).getTime()) / 86_400_000);
  if (days <= 0) return "今天";
  if (days === 1) return "昨天";
  if (days < 30) return `${days} 天前`;
  if (days < 365) return `${Math.floor(days / 30)} 个月前`;
  return `${Math.floor(days / 365)} 年前`;
}

export function formatNumber(value?: number | null) {
  return value == null ? "" : value.toLocaleString("zh-CN");
}

const COMPACT_FROM = 10_000;

/** A person's count at a glance: exact below 10,000, then "9.2 万", whole 万 from a million on. */
export function formatCount(value?: number | null) {
  if (value == null || value < COMPACT_FROM) return formatNumber(value);
  const wan = value / 10_000;
  return `${wan < 100 ? String(Math.floor(wan * 10) / 10) : Math.floor(wan).toLocaleString("zh-CN")} 万`;
}

/** The exact figure for a tooltip, only when `formatCount` rounded it. */
export const exactCount = (value?: number | null) => value != null && value >= COMPACT_FROM ? formatNumber(value) : undefined;

export function formatDateTime(value?: string | number | null, withTime = true): string {
  if (value == null || value === "") return "未记录时间";
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", withTime ? { dateStyle: "medium", timeStyle: "short" } : { dateStyle: "medium" }).format(date);
}
