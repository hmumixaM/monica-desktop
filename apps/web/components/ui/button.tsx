import Link from "next/link";
import type { ButtonHTMLAttributes, ComponentProps, ReactNode } from "react";

import styles from "./controls.module.css";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "highlight" | "danger";
type Size = "sm" | "md" | "lg";

export function buttonClass(variant: ButtonVariant = "secondary", size: Size = "md", block = false, extra?: string) {
  return [styles.button, styles[variant], size !== "md" && styles[size], block && styles.block, extra].filter(Boolean).join(" ");
}

export function Button({ variant, size, block, className, type = "button", ...props }: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant; size?: Size; block?: boolean;
}) {
  return <button type={type} className={buttonClass(variant, size, block, className)} {...props} />;
}

export function ButtonLink({ variant, size, block, className, ...props }: ComponentProps<typeof Link> & {
  variant?: ButtonVariant; size?: Size; block?: boolean;
}) {
  return <Link className={buttonClass(variant, size, block, className)} {...props} />;
}

/** A square icon button; `label` is required because the icon alone has no text. */
export function IconButton({ label, size, outlined, className, children, type = "button", ...props }:
  Omit<ButtonHTMLAttributes<HTMLButtonElement>, "aria-label"> & { label: string; size?: "sm" | "md"; outlined?: boolean; children: ReactNode }) {
  return <button type={type} aria-label={label} title={props.title ?? label} data-outlined={outlined ? "" : undefined}
    className={[styles.icon, size === "sm" && styles.sm, className].filter(Boolean).join(" ")} {...props}>{children}</button>;
}

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className={styles.kbd}>{children}</kbd>;
}
