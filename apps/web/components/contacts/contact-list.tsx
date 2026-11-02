"use client";

import { defaultRangeExtractor, type Range, useWindowVirtualizer, type Virtualizer } from "@tanstack/react-virtual";
import { useRouter } from "next/navigation";
import { type Ref, useCallback, useImperativeHandle, useLayoutEffect, useMemo, useRef, useState } from "react";

import { type DirectoryProfile, matchedGroupName } from "../../lib/directory";
import { transitionTo } from "../../lib/transitions";
import { PersonLink } from "../ui/person-link";
import { Avatar, exactCount, formatCount, relativeDays, SourceMark } from "../ui/primitives";
import styles from "./contacts.module.css";

const ROW = 60;
const LETTER = 30;
const COLUMNS = ["姓名", "工作", "城市", "来源", "最近联系", "消息", "标签"];

type Item = { kind: "letter"; letter: string } | { kind: "row"; profile: DirectoryProfile; row: number };
export interface ContactListHandle { jumpTo: (letter: string) => void; open: (row: number) => void }

function background(profile: DirectoryProfile, query: string) {
  const group = matchedGroupName(profile, query);
  if (group) return `群内：${group}`;
  if (profile.type === "group") return profile.memberCount ? `${profile.memberCount} 位成员` : "微信群";
  const alias = profile.aliases.find((value) => !value.startsWith("wxid_") && /[\p{L}\p{N}]/u.test(value));
  return [profile.headline, profile.summary, profile.city, alias].find(Boolean) ?? "";
}

/**
 * Every matching profile in one window-scrolled virtual list. With `letters` (name order) rows are grouped under
 * sticky letter headings; `active` is the keyboard-selected row.
 */
export function ContactList({ rows, letters, tags, query, active, handle }: {
  rows: DirectoryProfile[]; letters?: Map<string, string>; tags?: Map<string, string>; query: string; active: number;
  handle: Ref<ContactListHandle>;
}) {
  const router = useRouter();
  const [stickyTop, setStickyTop] = useState(0);
  const list = useRef<HTMLDivElement>(null);
  const [margin, setMargin] = useState(0);

  const { items, letterAt, rowAt } = useMemo(() => {
    const items: Item[] = [];
    const letterAt = new Map<string, number>();
    const rowAt: number[] = [];
    rows.forEach((profile, row) => {
      const letter = letters?.get(profile.id);
      if (letter && !letterAt.has(letter)) {
        letterAt.set(letter, items.length);
        items.push({ kind: "letter", letter });
      }
      rowAt[row] = items.length;
      items.push({ kind: "row", profile, row });
    });
    return { items, letterAt, rowAt };
  }, [rows, letters]);
  const headings = useMemo(() => [...letterAt.values()], [letterAt]);
  const activeHeading = useRef(0);
  const instance = useRef<Virtualizer<Window, Element>>(null);
  // The heading that owns the first row below the sticky header, not the row hidden under it.
  const rangeExtractor = useCallback((range: Range) => {
    if (!headings.length) return defaultRangeExtractor(range);
    const measured = instance.current?.measurementsCache;
    const line = (instance.current?.scrollOffset ?? 0) + stickyTop + 1;
    activeHeading.current = [...headings].reverse().find((index) => (measured?.[index]?.start ?? Infinity) <= line) ?? headings[0];
    return [...new Set([activeHeading.current, ...defaultRangeExtractor(range)])].sort((a, b) => a - b);
  }, [headings, stickyTop]);

  useLayoutEffect(() => {
    const measure = () => {
      setMargin(list.current!.getBoundingClientRect().top + window.scrollY);
      const bar = document.querySelector<HTMLElement>("[data-mobile-bar]");
      const head = list.current?.previousElementSibling;
      setStickyTop((bar?.getBoundingClientRect().height ?? 0) + (head?.getBoundingClientRect().height ?? 0));
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(document.body);
    return () => observer.disconnect();
  }, []);

  const virtualizer = useWindowVirtualizer({
    count: items.length, estimateSize: (index) => items[index].kind === "letter" ? LETTER : ROW, overscan: 12,
    scrollMargin: margin, scrollPaddingStart: stickyTop + (headings.length ? LETTER : 0), rangeExtractor,
  });
  instance.current = virtualizer;

  function open(row: number) {
    const profile = rows[row];
    if (!profile) return;
    const source = list.current?.querySelector<HTMLElement>(`[data-row="${row}"] [data-avatar]`);
    transitionTo(() => router.push(`/profiles/${profile.id}`), { source, readySelector: "[data-profile-hero]" });
  }
  useImperativeHandle(handle, () => ({
    jumpTo(letter) {
      const index = letterAt.get(letter) ?? [...letterAt.entries()].find(([key]) => key > letter)?.[1];
      if (index != null) window.scrollTo({ top: virtualizer.measurementsCache[index].start - stickyTop });
    },
    open,
  }));
  useLayoutEffect(() => {
    if (active >= 0 && rowAt[active] != null) virtualizer.scrollToIndex(rowAt[active], { align: "auto" });
  }, [active]);

  const tagNames = (profile: DirectoryProfile) => profile.tagIds.slice(0, 2).map((id) => tags?.get(id)).filter(Boolean).join("、")
    + (profile.tagIds.length > 2 ? ` +${profile.tagIds.length - 2}` : "");

  return <div className={styles.table} role="table" aria-rowcount={rows.length + 1} aria-label="联系人" data-contact-list="">
    <div className={styles.head} role="row">{COLUMNS.map((label) => <span role="columnheader" key={label}>{label}</span>)}</div>
    <div ref={list} className={styles.body} style={{ blockSize: virtualizer.getTotalSize() }}>
      {virtualizer.getVirtualItems().map((virtual) => {
        const item = items[virtual.index];
        const sticky = item.kind === "letter" && activeHeading.current === virtual.index && headings.length > 0;
        const style = sticky ? { position: "sticky" as const, insetBlockStart: stickyTop, blockSize: LETTER }
          : { transform: `translateY(${virtual.start - margin}px)`, blockSize: virtual.size };
        if (item.kind === "letter") return <div key={`l-${item.letter}`} className={styles.letter} data-sticky={sticky ? "" : undefined}
          style={style} role="row" aria-label={item.letter}><span role="rowheader">{item.letter}</span></div>;
        const { profile, row } = item;
        return <PersonLink key={profile.id} href={`/profiles/${profile.id}`} role="row" aria-rowindex={row + 2} className={styles.row}
          data-row={row} data-active={row === active ? "" : undefined} style={style}>
          <span role="cell" className={styles.name}>
            <Avatar name={profile.name} mediaId={profile.avatarMediaId} size={38} round={profile.type === "person"} />
            <span><strong>{profile.name}</strong><small>{background(profile, query)}</small></span>
          </span>
          <span role="cell">{profile.type === "group" ? "" : profile.headline}</span>
          <span role="cell">{profile.city}</span>
          <span role="cell" className={styles.sources}>{profile.providers.filter((provider) => provider !== "monica")
            .map((provider) => <SourceMark key={provider} provider={provider} size={11} />)}</span>
          <span role="cell" className={styles.last}>{relativeDays(profile.lastContactAt)}</span>
          <span role="cell" className={styles.number} title={exactCount(profile.directMessageCount)}>{formatCount(profile.directMessageCount)}</span>
          <span role="cell" className={styles.tags}>{tagNames(profile)}</span>
        </PersonLink>;
      })}
    </div>
  </div>;
}
