"use client";

import { useState } from "react";

import { useTagCounts } from "../../lib/directory";
import { ChipButton } from "../ui/chip";
import { SearchInput } from "../ui/field";
import styles from "./contacts.module.css";

/** Tags with member counts; several selected tags narrow to people carrying all of them. Shown when opened from the toolbar. */
export function TagBrowser({ type, selected, onChange }: { type?: "person" | "group"; selected: string[]; onChange: (ids: string[]) => void }) {
  const { tags, counts } = useTagCounts(type, selected);
  const [search, setSearch] = useState("");
  if (!tags?.size) return null;
  const needle = search.trim().toLocaleLowerCase();
  const rows = [...tags].map(([id, name]) => ({ id, name, count: counts.get(id) ?? 0, on: selected.includes(id) }))
    .filter((tag) => !tag.on && tag.count > 0 && tag.name.toLocaleLowerCase().includes(needle))
    .sort((a, b) => b.count - a.count);
  return <section className={styles.tagBrowser} aria-label="按标签查找">
    <SearchInput value={search} onChange={(event) => setSearch(event.target.value)} placeholder={`在 ${tags.size} 个标签里找`}
      aria-label="搜索标签" wrapperClassName={styles.tagSearch} />
    <p className={styles.tagHint}>{selected.length ? "再选一个，只留下同时带有这些标签的人" : "选一个标签，只看带有它的人"}</p>
    <div className={styles.tagList}>
      {rows.map((tag) => <ChipButton key={tag.id} count={tag.count} onClick={() => onChange([...selected, tag.id])}>{tag.name}</ChipButton>)}
      {!rows.length && <span className={styles.tagHint}>没有匹配的标签</span>}
    </div>
  </section>;
}
