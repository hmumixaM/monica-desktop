"use client";

import { useSyncExternalStore } from "react";

export interface RecentPerson { id: string; name: string; avatarMediaId: string | null; type: "person" | "group" }

const STORE_KEY = "monica.recent.v1";
const EVENT = "monica:recent";
const LIMIT = 8;
const EMPTY: RecentPerson[] = [];

let cachedRaw: string | null = null;
let cached: RecentPerson[] = EMPTY;

function read(): RecentPerson[] {
  const raw = localStorage.getItem(STORE_KEY);
  if (raw !== cachedRaw) {
    cachedRaw = raw;
    cached = raw ? JSON.parse(raw) as RecentPerson[] : EMPTY;
  }
  return cached;
}

/** Moves a profile to the front of the recently viewed list (at most eight). */
export function rememberPerson(person: RecentPerson) {
  const current = read();
  if (current[0]?.id === person.id && current[0].name === person.name && current[0].avatarMediaId === person.avatarMediaId) return;
  const next = [person, ...current.filter((item) => item.id !== person.id)].slice(0, LIMIT);
  localStorage.setItem(STORE_KEY, JSON.stringify(next));
  window.dispatchEvent(new Event(EVENT));
}

export function forgetRecent() {
  localStorage.removeItem(STORE_KEY);
  window.dispatchEvent(new Event(EVENT));
}

function subscribe(notify: () => void) {
  window.addEventListener(EVENT, notify);
  window.addEventListener("storage", notify);
  return () => {
    window.removeEventListener(EVENT, notify);
    window.removeEventListener("storage", notify);
  };
}

export function useRecentPeople(): RecentPerson[] {
  return useSyncExternalStore(subscribe, read, () => EMPTY);
}
