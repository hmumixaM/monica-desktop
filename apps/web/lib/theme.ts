"use client";

import { useSyncExternalStore } from "react";

export type ThemeChoice = "system" | "light" | "dark";

const STORE_KEY = "monica.theme.v1";
const EVENT = "monica:theme";

export const SIDEBAR_KEY = "monica.sidebar.v1";

/** Runs before first paint so a stored theme or a collapsed sidebar never flashes the other state. */
export const THEME_BOOTSTRAP = `try{var d=document.documentElement,t=localStorage.getItem("${STORE_KEY}");if(t==="light"||t==="dark")d.dataset.theme=t;if(localStorage.getItem("${SIDEBAR_KEY}")==="collapsed")d.dataset.sidebar="collapsed"}catch(e){}`;

function read(): ThemeChoice {
  const value = localStorage.getItem(STORE_KEY);
  return value === "light" || value === "dark" ? value : "system";
}

export function setTheme(choice: ThemeChoice) {
  if (choice === "system") {
    localStorage.removeItem(STORE_KEY);
    delete document.documentElement.dataset.theme;
  } else {
    localStorage.setItem(STORE_KEY, choice);
    document.documentElement.dataset.theme = choice;
  }
  window.dispatchEvent(new Event(EVENT));
}

function subscribe(notify: () => void) {
  const media = matchMedia("(prefers-color-scheme: dark)");
  window.addEventListener(EVENT, notify);
  window.addEventListener("storage", notify);
  media.addEventListener("change", notify);
  return () => {
    window.removeEventListener(EVENT, notify);
    window.removeEventListener("storage", notify);
    media.removeEventListener("change", notify);
  };
}

/** The stored choice ("system" follows the OS). */
export function useThemeChoice(): ThemeChoice {
  return useSyncExternalStore(subscribe, read, () => "system");
}

/** The palette actually shown, for canvases and maps that paint colours themselves. */
export function useResolvedTheme(): "light" | "dark" {
  return useSyncExternalStore(subscribe, resolvedTheme, () => "light");
}

export function resolvedTheme(): "light" | "dark" {
  const choice = read();
  if (choice !== "system") return choice;
  return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function cycleTheme() {
  const order: ThemeChoice[] = ["system", "light", "dark"];
  setTheme(order[(order.indexOf(read()) + 1) % order.length]);
}

/** Reads a token such as `--source-wechat` from the current palette. */
export function cssVar(name: string, element: Element = document.documentElement): string {
  return getComputedStyle(element).getPropertyValue(name).trim();
}
