import { Fragment } from "react";

const BEFORE = 36;
const LENGTH = 150;

/** The part of `text` around the first match, so a hit deep inside a long caption is still visible. */
export function excerpt(text: string, query: string): string {
  const flat = text.replace(/\s+/g, " ").trim();
  const at = flat.toLocaleLowerCase().indexOf(query.trim().toLocaleLowerCase());
  if (at <= BEFORE) return flat.length > LENGTH ? `${flat.slice(0, LENGTH)}…` : flat;
  const start = at - BEFORE;
  return `…${flat.slice(start, start + LENGTH)}${start + LENGTH < flat.length ? "…" : ""}`;
}

/** Marks every case-insensitive occurrence of `query`. */
export function Highlight({ text, query }: { text: string; query: string }) {
  const needle = query.trim().toLocaleLowerCase();
  if (!needle) return <>{text}</>;
  const parts: { text: string; hit: boolean }[] = [];
  const lower = text.toLocaleLowerCase();
  let from = 0;
  for (let at = lower.indexOf(needle); at >= 0; at = lower.indexOf(needle, from)) {
    if (at > from) parts.push({ text: text.slice(from, at), hit: false });
    parts.push({ text: text.slice(at, at + needle.length), hit: true });
    from = at + needle.length;
  }
  if (from < text.length) parts.push({ text: text.slice(from), hit: false });
  return <>{parts.map((part, index) => part.hit ? <mark key={index}>{part.text}</mark> : <Fragment key={index}>{part.text}</Fragment>)}</>;
}
