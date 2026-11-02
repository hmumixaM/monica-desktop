"use client";

import { LayoutGrid, Plus, Rows3, Tags } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { type KeyboardEvent, useDeferredValue, useEffect, useMemo, useRef, useState } from "react";

import { type DirectorySort, useDirectorySearch } from "../../lib/directory";
import { initialLetter, LETTERS, sortingName, usePinyin } from "../../lib/letters";
import { Button, Kbd } from "../ui/button";
import { Chip } from "../ui/chip";
import { SearchInput, Select } from "../ui/field";
import { PersonLink } from "../ui/person-link";
import { Avatar, EmptyState, formatNumber, Page, PageHeader, Skeleton } from "../ui/primitives";
import { Segmented } from "../ui/segmented";
import { ContactList, type ContactListHandle } from "./contact-list";
import styles from "./contacts.module.css";
import { CreateProfileDialog } from "./create-profile";
import { LetterIndex } from "./letter-index";
import { TagBrowser } from "./tag-filter";

const SORTS: { id: DirectorySort; label: string }[] = [
  { id: "relevance", label: "默认" }, { id: "last_contact", label: "最近联系" }, { id: "messages", label: "消息最多" },
  { id: "name", label: "姓名 A–Z" }, { id: "birthday", label: "生日临近" },
];
type Kind = "all" | "person" | "group";
const collator = new Intl.Collator("zh-Hans-CN-u-co-pinyin", { sensitivity: "base", numeric: true });

export function ContactsView() {
  const params = useSearchParams();
  const router = useRouter();
  const [text, setText] = useState(params.get("search") ?? params.get("q") ?? "");
  const query = useDeferredValue(text);
  const type = (params.get("type") as "person" | "group" | null) ?? undefined;
  const tagIds = params.getAll("tag");
  const companyId = params.get("company") ?? undefined;
  const schoolId = params.get("school") ?? undefined;
  const sort = (params.get("sort") as DirectorySort | null) ?? (query ? "relevance" : "last_contact");
  const view = params.get("view") === "grid" ? "grid" : "table";
  const { directory, results, isPending } = useDirectorySearch(query, { type, tagIds, companyId, schoolId }, sort);
  const [tagsOpen, setTagsOpen] = useState(false);
  const [creating, setCreating] = useState(params.get("create") === "1");
  const [active, setActive] = useState(-1);
  const [pendingLetter, setPendingLetter] = useState<string | null>(null);
  const list = useRef<ContactListHandle>(null);

  const byName = sort === "name" || (sort === "relevance" && !query.trim());
  const pinyin = usePinyin(view === "table");
  const { rows, letters } = useMemo(() => {
    if (!pinyin || !byName) return { rows: results, letters: undefined };
    const letters = new Map(results.map((profile) => [profile.id, initialLetter(profile.name, pinyin)]));
    const order = (id: string) => LETTERS.indexOf(letters.get(id)!);
    const names = new Map(results.map((profile) => [profile.id, sortingName(profile.name)]));
    const rows = [...results].sort((a, b) => order(a.id) - order(b.id) || collator.compare(names.get(a.id)!, names.get(b.id)!));
    return { rows, letters };
  }, [results, pinyin, byName]);
  const present = useMemo(() => new Set(letters?.values() ?? []), [letters]);

  function navigate(next: URLSearchParams) {
    router.replace(`/profiles${next.size ? `?${next}` : ""}`, { scroll: false });
  }
  function set(name: string, value?: string) {
    const next = new URLSearchParams(params);
    if (value) next.set(name, value); else next.delete(name);
    navigate(next);
  }
  function setTags(ids: string[]) {
    const next = new URLSearchParams(params);
    next.delete("tag");
    for (const id of ids) next.append("tag", id);
    navigate(next);
  }
  function jump(letter: string) {
    if (letters) list.current?.jumpTo(letter);
    else {
      setPendingLetter(letter);
      set("sort", "name");
    }
  }
  useEffect(() => {
    if (!pendingLetter || !letters) return;
    list.current?.jumpTo(pendingLetter);
    setPendingLetter(null);
  }, [pendingLetter, letters]);
  useEffect(() => setActive(query.trim() ? 0 : -1), [query, type, sort, tagIds.join(","), companyId, schoolId]);
  const keyHandler = useRef(onKey);
  keyHandler.current = onKey;
  useEffect(() => {
    const listener = (event: globalThis.KeyboardEvent) => { if (event.target === document.body) keyHandler.current(event); };
    window.addEventListener("keydown", listener);
    return () => window.removeEventListener("keydown", listener);
  }, []);

  function onKey(event: KeyboardEvent | globalThis.KeyboardEvent) {
    const target = event.target as HTMLElement;
    const ours = target === document.body || target.matches("[data-slash-target]") || target.closest("[data-contact-list]");
    if (view !== "table" || !rows.length || !ours) return;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const step = event.key === "ArrowDown" ? 1 : -1;
      setActive((index) => Math.max(0, Math.min(rows.length - 1, index + step)));
    } else if (event.key === "Enter" && active >= 0) {
      event.preventDefault();
      list.current?.open(active);
    }
  }

  const filterName = companyId ? directory?.companies.get(companyId) : schoolId ? directory?.schools.get(schoolId) : undefined;
  const chips = tagIds.length > 0 || filterName;

  return <Page width="wide" className={styles.page}><div className={styles.frame} onKeyDown={onKey}>
    <PageHeader title="联系人" count={directory ? formatNumber(results.length) : ""}
      actions={<Button variant="primary" onClick={() => setCreating(true)}><Plus size={15} />新建</Button>} />
    <div className={styles.toolbar}>
      <SearchInput value={text} onChange={(event) => setText(event.target.value)} placeholder="姓名、备注、拼音或首字母"
        aria-label="筛选联系人" data-slash-target="" wrapperClassName={styles.filter} trailing={<Kbd>/</Kbd>} />
      <Segmented<Kind> label="类型" value={type ?? "all"} onChange={(value) => set("type", value === "all" ? undefined : value)}
        options={[{ value: "all", label: "全部" }, { value: "person", label: "人" }, { value: "group", label: "群" }]} />
      <Select value={sort} onChange={(event) => set("sort", event.target.value)} aria-label="排序" className={styles.sort}>
        {SORTS.map((option) => <option key={option.id} value={option.id}>{option.label}</option>)}
      </Select>
      <Button variant={tagsOpen ? "highlight" : "secondary"} aria-expanded={tagsOpen} onClick={() => setTagsOpen((open) => !open)}>
        <Tags size={15} />标签{tagIds.length > 0 && <b className={styles.badge}>{tagIds.length}</b>}
      </Button>
      <Segmented<"table" | "grid"> label="视图" value={view} onChange={(value) => set("view", value === "grid" ? "grid" : undefined)}
        className={styles.views}
        options={[{ value: "table", label: <Rows3 size={16} aria-label="列表" /> }, { value: "grid", label: <LayoutGrid size={16} aria-label="头像墙" /> }]} />
    </div>
    {chips && <div className={styles.chips} aria-label="当前筛选">
      {tagIds.map((id) => <Chip key={id} active onRemove={() => setTags(tagIds.filter((tag) => tag !== id))} removeLabel="移除这个标签">
        {directory?.tags.get(id) ?? "已删除的标签"}</Chip>)}
      {filterName && <Chip active onRemove={() => { const next = new URLSearchParams(params); next.delete("company"); next.delete("school"); navigate(next); }}
        removeLabel="清除">{companyId ? "公司" : "学校"}：{filterName}</Chip>}
      {tagIds.length > 1 && <span className={styles.tagHint}>同时带有这些标签</span>}
      <button type="button" className={styles.clear} onClick={() => {
        const next = new URLSearchParams(params);
        for (const name of ["tag", "company", "school"]) next.delete(name);
        navigate(next);
      }}>全部清除</button>
    </div>}
    {tagsOpen && <TagBrowser type={type} selected={tagIds} onChange={setTags} />}

    {isPending && !directory ? <Skeleton height="60dvh" />
      : !results.length ? <EmptyState title={query ? `没有找到「${query}」` : "这里还没有人"}>
        {query ? "试试拼音、首字母，或对方在群里的昵称。" : "换一个筛选条件看看。"}</EmptyState>
      : view === "grid"
        ? <div className={styles.grid}>{results.slice(0, 600).map((profile) => <PersonLink key={profile.id} href={`/profiles/${profile.id}`}>
          <Avatar name={profile.name} mediaId={profile.avatarMediaId} size={64} round={profile.type === "person"} /><strong>{profile.name}</strong>
        </PersonLink>)}</div>
        : <div className={styles.listArea}>
          <ContactList rows={rows} letters={letters} tags={directory?.tags} query={query} active={active} handle={list} />
          {rows.length > 30 && <LetterIndex present={letters ? present : new Set(LETTERS)} onJump={jump} />}
        </div>}
    {creating && <CreateProfileDialog onClose={() => {
      setCreating(false);
      if (params.get("create")) set("create");
    }} />}
  </div></Page>;
}
