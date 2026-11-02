"use client";

import { useEffect, useState } from "react";

export const LETTERS = [..."ABCDEFGHIJKLMNOPQRSTUVWXYZ", "#"];

type Pinyin = typeof import("pinyin-pro").pinyin;
let pinyin: Pinyin | undefined;
let loading: Promise<Pinyin> | undefined;

/** pinyin-pro is large; it loads only when a letter index is shown. */
export function usePinyin(active: boolean): Pinyin | undefined {
  const [loaded, setLoaded] = useState(() => pinyin);
  useEffect(() => {
    if (!active || loaded) return;
    loading ??= import("pinyin-pro").then((module) => (pinyin = module.pinyin));
    void loading.then((value) => setLoaded(() => value));
  }, [active, loaded]);
  return loaded;
}

const LEADING = /[A-Za-z0-9]|\p{Script=Han}/u;

/** The name from its first letter, digit or Chinese character on, so emoji and punctuation do not decide the order. */
export function sortingName(name: string): string {
  const start = name.search(LEADING);
  return start > 0 ? name.slice(start) : name;
}

/** The index letter of a name: its first Latin letter, or the pinyin initial of its first Chinese character; digits and the rest go under "#". */
export function initialLetter(name: string, toPinyin: Pinyin): string {
  const first = name.match(LEADING)?.[0];
  if (!first || /[0-9]/.test(first)) return "#";
  if (/[A-Za-z]/.test(first)) return first.toUpperCase();
  const letter = toPinyin(first, { pattern: "first", toneType: "none", surname: "head" }).toUpperCase();
  return /^[A-Z]$/.test(letter) ? letter : "#";
}
