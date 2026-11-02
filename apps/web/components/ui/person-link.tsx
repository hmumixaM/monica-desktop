"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import type { ComponentProps, MouseEvent } from "react";

import { isModifiedClick, transitionTo } from "../../lib/transitions";

/**
 * A link to a profile whose avatar (the element marked `data-avatar` inside it) morphs into the profile hero.
 * Modified clicks still open a new tab as usual.
 */
export function PersonLink({ href, onClick, ...props }: ComponentProps<typeof Link> & { href: string }) {
  const router = useRouter();
  function open(event: MouseEvent<HTMLAnchorElement>) {
    onClick?.(event);
    if (event.defaultPrevented || isModifiedClick(event)) return;
    event.preventDefault();
    const source = event.currentTarget.querySelector<HTMLElement>("[data-avatar]");
    transitionTo(() => router.push(href), { source, readySelector: "[data-profile-hero]" });
  }
  return <Link href={href} onClick={open} {...props} />;
}
