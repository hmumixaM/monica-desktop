"use client";

import { MessagesSquare, NotebookPen, Search } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { type ReactNode, useDeferredValue, useEffect, useMemo, useState } from "react";

import type { SearchHit } from "../../lib/api-types";
import { useDirectorySearch, usePostSearch, useTagCounts } from "../../lib/directory";
import { useServerSearch } from "../../lib/queries";
import { Button } from "../ui/button";
import { ChipLink } from "../ui/chip";
import { SearchInput } from "../ui/field";
import { PersonLink } from "../ui/person-link";
import { Avatar, EmptyState, formatDateTime, Page, PageHeader, relativeDays, SourceMark } from "../ui/primitives";
import { Segmented } from "../ui/segmented";
import { excerpt, Highlight } from "./highlight";
import styles from "./search.module.css";

type Kind = "all" | "people" | "tags" | "posts" | "records";
const KINDS: Kind[] = ["all", "people", "tags", "posts", "records"];
const LABEL: Record<Kind, string> = { all: "全部", people: "联系人", tags: "标签", posts: "动态", records: "记录" };
const PREVIEW: Record<Exclude<Kind, "all">, number> = { people: 8, tags: 16, posts: 6, records: 5 };

interface PersonHit { id: string; name: string; type: "person" | "group"; avatarMediaId: string | null; detail: string; lastContactAt?: string | null }
interface PostHit { id: string; profileId: string | null; author: string; avatarMediaId: string | null; provider: string; occurredAt: string | null; text: string }

function Group({ kind, count, shown, onMore, children, icon }: {
  kind: Exclude<Kind, "all">; count: number; shown: number; onMore?: () => void; children: ReactNode; icon?: ReactNode;
}) {
  if (!count) return null;
  return <section className={styles.group} aria-labelledby={`search-${kind}`}>
    <header><h2 id={`search-${kind}`}>{icon}{LABEL[kind]}<small>{count}</small></h2>
      {onMore && count > shown && <Button size="sm" variant="ghost" onClick={onMore}>全部 {count} 条</Button>}</header>
    {children}
  </section>;
}

export function SearchView() {
  const params = useSearchParams();
  const router = useRouter();
  const [text, setText] = useState(params.get("q") ?? "");
  const query = useDeferredValue(text.trim());
  const kind = KINDS.includes(params.get("type") as Kind) ? params.get("type") as Kind : "all";

  useEffect(() => {
    const timer = setTimeout(() => {
      if ((params.get("q") ?? "") === query) return;
      const next = new URLSearchParams(params);
      if (query) next.set("q", query); else next.delete("q");
      router.replace(`/search?${next}`, { scroll: false });
    }, 300);
    return () => clearTimeout(timer);
  }, [query, params, router]);
  function setKind(value: Kind) {
    const next = new URLSearchParams(params);
    if (value === "all") next.delete("type"); else next.set("type", value);
    router.replace(`/search?${next}`, { scroll: false });
  }

  const localSearch = useDirectorySearch(query);
  const { directory, results: local } = localSearch;
  const { tags, counts } = useTagCounts();
  const posts = usePostSearch(query, query.length > 0, 200);
  const server = useServerSearch(query);
  const remote = server.data ?? [];

  const people = useMemo<PersonHit[]>(() => {
    if (!query) return [];
    const rows: PersonHit[] = local.map((profile) => ({
      id: profile.id, name: profile.name, type: profile.type, avatarMediaId: profile.avatarMediaId, lastContactAt: profile.lastContactAt,
      detail: profile.type === "group" ? (profile.memberCount ? `${profile.memberCount} 位成员` : "群") : [profile.headline, profile.city].filter(Boolean).join(" · "),
    }));
    const seen = new Set(rows.map((row) => row.id));
    for (const hit of remote.filter((row) => row.entity_type === "profile" && !seen.has(row.id))) {
      const profile = directory?.byId.get(hit.id);
      rows.push({ id: hit.id, name: hit.title, type: profile?.type ?? (hit.kind === "group" ? "group" : "person"),
        avatarMediaId: profile?.avatarMediaId ?? hit.avatar_media_id ?? null, detail: hit.summary, lastContactAt: profile?.lastContactAt });
    }
    return rows;
  }, [query, local, remote, directory]);

  const tagHits = useMemo(() => {
    const needle = query.toLocaleLowerCase();
    return needle && tags ? [...tags].filter(([, name]) => name.toLocaleLowerCase().includes(needle))
      .map(([id, name]) => ({ id, name, count: counts.get(id) ?? 0 })).sort((a, b) => b.count - a.count) : [];
  }, [query, tags, counts]);

  const postHits = useMemo<PostHit[]>(() => {
    const author = (id: string | null) => (id ? directory?.byId.get(id) : undefined);
    const rows: PostHit[] = posts.results.map((post) => ({ ...post, author: author(post.profileId)?.name ?? "", avatarMediaId: author(post.profileId)?.avatarMediaId ?? null }));
    const seen = new Set(rows.map((row) => row.id));
    for (const hit of remote.filter((row) => row.entity_type === "timeline" && !seen.has(row.id))) {
      rows.push({ id: hit.id, profileId: hit.profile_id ?? null, author: hit.profile_name ?? author(hit.profile_id ?? null)?.name ?? "",
        avatarMediaId: hit.avatar_media_id ?? author(hit.profile_id ?? null)?.avatarMediaId ?? null,
        provider: hit.provider ?? hit.title.split(" · ")[0], occurredAt: hit.occurred_at ?? null, text: hit.summary });
    }
    return rows.sort((a, b) => (b.occurredAt ?? "").localeCompare(a.occurredAt ?? ""));
  }, [posts.results, remote, directory]);

  const records = remote.filter((row) => row.entity_type === "activity");
  const total = { people: people.length, tags: tagHits.length, posts: postHits.length, records: records.length };
  const limit = (group: Exclude<Kind, "all">) => (kind === "all" ? PREVIEW[group] : Infinity);
  const show = (group: Exclude<Kind, "all">) => kind === "all" || kind === group;
  const nothing = query && !server.isFetching && !posts.isFetching && !localSearch.isPending && !localSearch.isError && !posts.isError && !server.isError && Object.values(total).every((count) => count === 0);

  return <Page width="narrow" className={styles.page}>
    <PageHeader title="搜索" />
    <SearchInput value={text} onChange={(event) => setText(event.target.value)} placeholder="搜索联系人、标签、动态或记录" aria-label="搜索"
      autoFocus={!text} data-slash-target="" wrapperClassName={styles.input} />
    {query && <Segmented<Kind> label="结果类型" value={kind} onChange={setKind} className={styles.kinds}
      options={KINDS.map((value) => ({ value, label: LABEL[value], count: value === "all" ? undefined : total[value], disabled: value !== "all" && !total[value] }))} />}

    {query && (localSearch.isPending || posts.isFetching || server.isFetching) && <p role="status">正在查找…</p>}
    {query && (localSearch.isError || posts.isError || server.isError) && <button type="button" onClick={() => { void localSearch.retrySearch(); void posts.refetch(); void server.refetch(); }}>部分搜索未能加载，点击重试</button>}
    {!query ? <EmptyState icon={<Search size={28} />} title="输入搜索词" />
      : nothing ? <EmptyState title={`没有找到「${query}」`}>
        聊天记录不在这里搜索：<Link href="/messages">打开对应会话</Link>后点放大镜。</EmptyState>
      : <div className={styles.results}>
        {show("people") && <Group kind="people" count={total.people} shown={limit("people")} onMore={() => setKind("people")}>
          <ul className={styles.people}>{people.slice(0, limit("people")).map((person) => <li key={person.id}>
            <PersonLink href={`/profiles/${person.id}`}>
              <Avatar name={person.name} mediaId={person.avatarMediaId} size={40} round={person.type === "person"} />
              <span className={styles.text}><strong><Highlight text={person.name} query={query} /></strong>
                {person.detail && <small><Highlight text={excerpt(person.detail, query)} query={query} /></small>}</span>
              {person.lastContactAt && <time>{relativeDays(person.lastContactAt)}</time>}
            </PersonLink>
          </li>)}</ul>
          {kind === "people" && <Link className={styles.more} href={`/profiles?q=${encodeURIComponent(query)}`}>在联系人列表里筛选、排序</Link>}
        </Group>}
        {show("tags") && <Group kind="tags" count={total.tags} shown={limit("tags")} onMore={() => setKind("tags")}>
          <div className={styles.tags}>{tagHits.slice(0, limit("tags")).map((tag) =>
            <ChipLink key={tag.id} href={`/profiles?tag=${tag.id}`} count={tag.count}>{tag.name}</ChipLink>)}</div>
        </Group>}
        {show("posts") && <Group kind="posts" count={total.posts} shown={limit("posts")} onMore={() => setKind("posts")}>
          <ul className={styles.posts}>{postHits.slice(0, limit("posts")).map((post) => <li key={post.id}>
            <Link href={`/timeline?event=${post.id}`}>
              <Avatar name={post.author || "?"} mediaId={post.avatarMediaId} size={32} round />
              <span className={styles.text}>
                <span className={styles.byline}><strong>{post.author || "未关联的作者"}</strong><SourceMark provider={post.provider} size={10} />
                  {post.occurredAt && <time>{formatDateTime(post.occurredAt, false)}</time>}</span>
                <p><Highlight text={excerpt(post.text, query)} query={query} /></p>
              </span>
            </Link>
          </li>)}</ul>
        </Group>}
        {show("records") && <Group kind="records" count={total.records} shown={limit("records")} onMore={() => setKind("records")} icon={<NotebookPen size={15} />}>
          <ul className={styles.records}>{records.slice(0, limit("records")).map((record) => <li key={record.id}><Record hit={record} query={query} /></li>)}</ul>
        </Group>}
        {(server.isFetching || posts.isFetching) && <p className={styles.status} role="status">还在找…</p>}
      </div>}
  </Page>;
}

function Record({ hit, query }: { hit: SearchHit; query: string }) {
  const body = <>
    <strong><Highlight text={hit.title} query={query} /></strong>
    {hit.summary && <p><Highlight text={excerpt(hit.summary, query)} query={query} /></p>}
    <small>{[hit.profile_name, hit.occurred_at && formatDateTime(hit.occurred_at, false)].filter(Boolean).join(" · ")}</small>
  </>;
  return hit.profile_id ? <Link href={`/profiles/${hit.profile_id}?tab=records`}>{body}</Link> : <div>{body}</div>;
}
