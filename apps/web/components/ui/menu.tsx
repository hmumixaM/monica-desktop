"use client";

import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import type { ReactNode } from "react";

import styles from "./overlay.module.css";

export interface MenuEntry { label: ReactNode; icon?: ReactNode; onSelect?: () => void; href?: string; disabled?: boolean; separator?: boolean }

/** A dropdown of actions; `trigger` must be a single focusable element such as an IconButton. */
export function Menu({ trigger, items, align = "end" }: { trigger: ReactNode; items: MenuEntry[]; align?: "start" | "end" }) {
  return <DropdownMenu.Root modal={false}>
    <DropdownMenu.Trigger asChild>{trigger}</DropdownMenu.Trigger>
    <DropdownMenu.Portal>
      <DropdownMenu.Content className={styles.menu} align={align} sideOffset={6} collisionPadding={8}>
        {items.map((item, index) => item.separator
          ? <DropdownMenu.Separator key={index} className={styles.menuSeparator} />
          : <DropdownMenu.Item key={index} className={styles.menuItem} disabled={item.disabled} asChild={!!item.href}
            onSelect={item.onSelect}>
            {item.href ? <a href={item.href}>{item.icon}{item.label}</a> : <span>{item.icon}{item.label}</span>}
          </DropdownMenu.Item>)}
      </DropdownMenu.Content>
    </DropdownMenu.Portal>
  </DropdownMenu.Root>;
}
