"use client";

import { Command } from "cmdk";
import { CornerDownLeft, Monitor, Moon, Plus, Search, Sun } from "lucide-react";
import { useRouter } from "next/navigation";
import { type ReactNode, useEffect, useRef, useState } from "react";

import { matchedGroupName, useDirectory, usePersonPicker, usePostSearch } from "../../lib/directory";
import { useRecentPeople } from "../../lib/recent";
import { setTheme } from "../../lib/theme";
import { transitionTo } from "../../lib/transitions";
import { Kbd } from "../ui/button";
import { Avatar, providerLabel, SourceMark } from "../ui/primitives";
import { DATA_NAV, DIGEST_NAV, PRIMARY_NAV } from "./nav";
import styles from "./palette.module.css";

/**
 * ⌘K / "/" palette. Results are grouped in a fixed order: recently viewed, people (pinyin, initials and WeChat
 * group names via the local directory), posts (local post index), then pages and commands. When something is
 * typed the first row searches everything, so Enter still opens /search?q= as the old search box did.
 */
export function CommandPalette({ onClose }: { onClose: () => void }) {
  const router = useRouter();
  const dialog = useRef<HTMLDialogElement>(null);
  const [text, setText] = useState("");
  const [composing, setComposing] = useState(false);
  const [committed, setCommitted] = useState("");
  const query = (composing ? committed : text).trim();
  const recent = useRecentPeople();
  const { directory } = useDirectory();
  const peopleSearch = usePersonPicker(query, 8);
  const people = peopleSearch.results;
  const postSearch = usePostSearch(query, query.length > 0, 5);
  const posts = postSearch.results;

  useEffect(() => {
    dialog.current?.showModal();
    const viewport = window.visualViewport;
    const resize = () => {
      dialog.current?.style.setProperty("--palette-height", `${viewport?.height ?? window.innerHeight}px`);
      dialog.current?.style.setProperty("--palette-top", `${viewport?.offsetTop ?? 0}px`);
    };
    resize();
    viewport?.addEventListener("resize", resize);
    viewport?.addEventListener("scroll", resize);
    return () => { viewport?.removeEventListener("resize", resize); viewport?.removeEventListener("scroll", resize); };
  }, []);

  function go(href: string, source?: HTMLElement | null) {
    onClose();
    if (href.startsWith("/profiles/")) transitionTo(() => router.push(href), { source, readySelector: "[data-profile-hero]" });
    else router.push(href);
  }
  function run(action: () => void) {
    action();
    onClose();
  }
  const sourceFor = (value: string) => document.querySelector<HTMLElement>(`[cmdk-item][data-value="${CSS.escape(value)}"] [data-avatar]`);

  const needle = query.toLocaleLowerCase();
  const matches = (label: string, keywords = "") => !needle || `${label} ${keywords}`.toLocaleLowerCase().includes(needle);
  const recentShown = recent.filter((person) => (!directory || directory.byId.has(person.id)) && matches(person.name)).slice(0, query ? 3 : 8);
  const pages = [...PRIMARY_NAV, DIGEST_NAV, ...DATA_NAV].filter((entry) => matches(entry.label, entry.keywords));
  const commands: { id: string; label: string; keywords: string; icon: ReactNode; action: () => void }[] = [
    { id: "theme-light", label: "浅色主题", keywords: "theme light 主题 白天", icon: <Sun size={16} />, action: () => run(() => setTheme("light")) },
    { id: "theme-dark", label: "深色主题", keywords: "theme dark 主题 夜间 暗色", icon: <Moon size={16} />, action: () => run(() => setTheme("dark")) },
    { id: "theme-system", label: "主题跟随系统", keywords: "theme system 主题 自动", icon: <Monitor size={16} />, action: () => run(() => setTheme("system")) },
    { id: "create", label: "新建联系人", keywords: "create new contact xinjian 添加", icon: <Plus size={16} />, action: () => go("/profiles?create=1") },
  ].filter((command) => matches(command.label, command.keywords));

  return <dialog ref={dialog} className={styles.palette} aria-label="命令面板" onCancel={(event) => { event.preventDefault(); onClose(); }}
    onClick={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <Command onKeyDownCapture={(event) => { if (event.nativeEvent.isComposing && event.key === "Enter") event.stopPropagation(); }} shouldFilter={false} loop label="命令面板" className={styles.command}>
      <div className={styles.inputRow}>
        <Search size={18} aria-hidden="true" />
        <Command.Input autoFocus value={text} onValueChange={setText} onCompositionStart={() => { setCommitted(text); setComposing(true); }}
          onCompositionEnd={(event) => { setText(event.currentTarget.value); setComposing(false); }} placeholder="找人、动态或页面…" />
        <button type="button" className={styles.cancel} onClick={onClose}>取消</button>
        <span className={styles.escape}><Kbd>esc</Kbd></span>
      </div>
      <Command.List className={styles.list}>
        {query && (peopleSearch.isSearching || postSearch.isFetching) && <p role="status" className={styles.empty}>正在查找…</p>}
        {(peopleSearch.searchError || postSearch.isError) && <button type="button" className={styles.empty}
          onClick={() => { void peopleSearch.retrySearch(); void postSearch.refetch(); }}>搜索加载失败，点击重试</button>}
        <Command.Empty className={styles.empty}>没有找到匹配的人或页面</Command.Empty>
        {query && <Command.Group>
          <Command.Item value="search-all" onSelect={() => go(`/search?q=${encodeURIComponent(query)}`)}>
            <span className={styles.glyph}><Search size={16} /></span>
            <span className={styles.label}><strong>搜索全部「{query}」</strong><small>联系人、标签、动态与记录</small></span>
            <CornerDownLeft size={14} className={styles.enter} aria-hidden="true" />
          </Command.Item>
        </Command.Group>}
        {recentShown.length > 0 && <Command.Group heading="最近查看">
          {recentShown.map((person) => <Command.Item key={`recent-${person.id}`} value={`recent-${person.id}`}
            onSelect={(value) => go(`/profiles/${person.id}`, sourceFor(value))}>
            <Avatar name={person.name} mediaId={person.avatarMediaId} size={28} />
            <span className={styles.label}><strong>{person.name}</strong>{person.type === "group" && <small>群组</small>}</span>
          </Command.Item>)}
        </Command.Group>}
        {people.length > 0 && <Command.Group heading="联系人">
          {people.map((person) => <Command.Item key={person.id} value={`person-${person.id}`}
            onSelect={(value) => go(`/profiles/${person.id}`, sourceFor(value))}>
            <Avatar name={person.name} mediaId={person.avatarMediaId} size={28} />
            <span className={styles.label}>
              <strong>{person.name}</strong>
              {matchedGroupName(person, query) ? <small>群内：{matchedGroupName(person, query)}</small>
                : (person.headline || person.city) && <small>{[person.headline, person.city].filter(Boolean).join(" · ")}</small>}
            </span>
          </Command.Item>)}
        </Command.Group>}
        {posts.length > 0 && <Command.Group heading="动态">
          {posts.map((post) => <Command.Item key={post.id} value={`post-${post.id}`} onSelect={() => go(`/timeline?event=${encodeURIComponent(post.id)}`)}>
            <SourceMark provider={post.provider} size={12} />
            <span className={styles.label}>
              <strong className={styles.snippet}>{post.text}</strong>
              <small>{[post.profileId && directory?.byId.get(post.profileId)?.name, providerLabel(post.provider), post.occurredAt?.slice(0, 10)].filter(Boolean).join(" · ")}</small>
            </span>
          </Command.Item>)}
        </Command.Group>}
        {(pages.length > 0 || commands.length > 0) && <Command.Group heading="页面与命令">
          {pages.map((entry) => <Command.Item key={entry.id} value={`page-${entry.id}`} onSelect={() => go(entry.href)}>
            <span className={styles.glyph}><entry.icon size={16} /></span><span className={styles.label}><strong>{entry.label}</strong></span>
          </Command.Item>)}
          {commands.map((command) => <Command.Item key={command.id} value={command.id} onSelect={command.action}>
            <span className={styles.glyph}>{command.icon}</span><span className={styles.label}><strong>{command.label}</strong></span>
          </Command.Item>)}
        </Command.Group>}
      </Command.List>
      <footer className={styles.footer}><span><Kbd>↑</Kbd><Kbd>↓</Kbd> 选择</span><span><Kbd>↵</Kbd> 打开</span><span><Kbd>⌘</Kbd><Kbd>K</Kbd> 随时打开</span></footer>
    </Command>
  </dialog>;
}
