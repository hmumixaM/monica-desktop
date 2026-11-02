"use client";

import { useQueryClient } from "@tanstack/react-query";
import { LogOut, Monitor, Moon, MoreHorizontal, PanelLeftClose, PanelLeftOpen, Search, Sun } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { type ReactNode, useCallback, useEffect, useState } from "react";

import { apiFetch } from "../lib/api";
import { clearClientData } from "../lib/queries";
import { forgetRecent, type RecentPerson, useRecentPeople } from "../lib/recent";
import { cycleTheme, SIDEBAR_KEY, type ThemeChoice, useThemeChoice } from "../lib/theme";
import styles from "./shell/shell.module.css";
import { CommandPalette } from "./shell/command-palette";
import { ACTIVE_ALIAS, DATA_NAV, DIGEST_NAV, type NavEntry, PRIMARY_NAV, TAB_NAV } from "./shell/nav";
import { IconButton, Kbd } from "./ui/button";
import { Sheet } from "./ui/dialog";
import { Avatar } from "./ui/primitives";
import { PersonLink } from "./ui/person-link";

const THEME_LABEL: Record<ThemeChoice, string> = { system: "跟随系统", light: "浅色", dark: "深色" };
const THEME_ICON: Record<ThemeChoice, ReactNode> = { system: <Monitor size={17} />, light: <Sun size={17} />, dark: <Moon size={17} /> };

function typingTarget(target: EventTarget | null) {
  const element = target as HTMLElement | null;
  return !!element && (element.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(element.tagName));
}

export function AppShell({ active, children }: { active: string; children: ReactNode }) {
  const router = useRouter();
  const client = useQueryClient();
  const recent = useRecentPeople();
  const theme = useThemeChoice();
  const [palette, setPalette] = useState(false);
  const [more, setMore] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const current = ACTIVE_ALIAS[active] ?? active;

  useEffect(() => {
    setCollapsed(document.documentElement.dataset.sidebar === "collapsed");
    fetch("/api/v1/me").then((response) => {
      if (response.status === 401) window.location.assign("/login");
    }).catch(() => undefined);
  }, []);

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPalette((open) => !open);
        return;
      }
      if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey || typingTarget(event.target)) return;
      event.preventDefault();
      // A page with its own filter box (contacts) claims "/" for it.
      const local = document.querySelector<HTMLInputElement>("[data-slash-target]");
      if (local) local.focus();
      else setPalette(true);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const toggleSidebar = useCallback(() => {
    setCollapsed((value) => {
      const next = !value;
      if (next) document.documentElement.dataset.sidebar = "collapsed";
      else delete document.documentElement.dataset.sidebar;
      localStorage.setItem(SIDEBAR_KEY, next ? "collapsed" : "open");
      return next;
    });
  }, []);

  async function logout() {
    await apiFetch("/api/v1/auth/logout", { method: "POST" });
    await clearClientData(client);
    forgetRecent();
    router.replace("/login");
  }

  const navLink = (entry: NavEntry, compact = false) => <Link key={entry.id} href={entry.href} className={styles.navItem}
    aria-current={current === entry.id ? "page" : undefined} title={compact ? entry.label : undefined}>
    <entry.icon size={18} strokeWidth={1.8} aria-hidden="true" /><span>{entry.label}</span>
  </Link>;

  return <div className={styles.frame}>
    <a className={styles.skip} href="#main-content">跳转到主要内容</a>
    <aside className={styles.sidebar} aria-label="主导航">
      <div className={styles.brandRow}>
        <Link className={styles.brand} href="/dashboard" aria-label="Monica 首页"><span>M</span><strong>Monica</strong></Link>
        <IconButton label={collapsed ? "展开侧栏" : "收起侧栏"} size="sm" onClick={toggleSidebar} className={styles.collapse}>
          {collapsed ? <PanelLeftOpen size={16} /> : <PanelLeftClose size={16} />}
        </IconButton>
      </div>
      <button type="button" className={styles.searchButton} onClick={() => setPalette(true)} aria-label="搜索（⌘K）">
        <Search size={16} aria-hidden="true" /><span>搜索</span><span className={styles.shortcut}><Kbd>⌘</Kbd><Kbd>K</Kbd></span>
      </button>
      <nav className={styles.nav}>{PRIMARY_NAV.map((entry) => navLink(entry, collapsed))}</nav>
      {recent.length > 0 && <section className={styles.recent} aria-labelledby="recent-heading">
        <h2 id="recent-heading">最近查看</h2>
        <RecentList people={recent} />
      </section>}
      <section className={styles.dataGroup} aria-labelledby="data-heading">
        <h2 id="data-heading">数据与设置</h2>
        <nav>{DATA_NAV.map((entry) => navLink(entry, collapsed))}</nav>
        <div className={styles.utility}>
          <button type="button" className={styles.navItem} onClick={cycleTheme} title={`主题：${THEME_LABEL[theme]}`}>
            {THEME_ICON[theme]}<span>主题：{THEME_LABEL[theme]}</span>
          </button>
          <button type="button" className={styles.navItem} onClick={() => void logout()} title="退出">
            <LogOut size={17} strokeWidth={1.8} aria-hidden="true" /><span>退出</span>
          </button>
        </div>
      </section>
    </aside>

    <header className={styles.mobileBar} data-mobile-bar="">
      <Link className={styles.brand} href="/dashboard" aria-label="Monica 首页"><span>M</span></Link>
      <button type="button" className={styles.mobileSearch} onClick={() => setPalette(true)}>
        <Search size={16} aria-hidden="true" /><span>找人、动态或页面</span>
      </button>
    </header>

    <main id="main-content" className={styles.main}>{children}</main>

    <nav className={styles.tabbar} aria-label="主导航">
      {TAB_NAV.map((entry) => <Link key={entry.id} href={entry.href} aria-current={current === entry.id ? "page" : undefined}>
        <entry.icon size={21} strokeWidth={1.8} aria-hidden="true" /><span>{entry.label}</span>
      </Link>)}
      <button type="button" onClick={() => setMore(true)} aria-current={TAB_NAV.some((entry) => entry.id === current) ? undefined : "page"}
        aria-haspopup="dialog">
        <MoreHorizontal size={21} strokeWidth={1.8} aria-hidden="true" /><span>更多</span>
      </button>
    </nav>

    {more && <Sheet label="更多" onClose={() => setMore(false)}>
      <div className={styles.moreGrid} onClick={(event) => { if ((event.target as HTMLElement).closest("a")) setMore(false); }}>
        {[...PRIMARY_NAV.slice(4), DIGEST_NAV, ...DATA_NAV].map((entry) => <Link key={entry.id} href={entry.href}
          aria-current={current === entry.id ? "page" : undefined}>
          <entry.icon size={20} strokeWidth={1.8} aria-hidden="true" /><span>{entry.label}</span>
        </Link>)}
      </div>
      {recent.length > 0 && <section className={styles.moreRecent}>
        <h2>最近查看</h2>
        <div onClick={(event) => { if ((event.target as HTMLElement).closest("a")) setMore(false); }}><RecentList people={recent} /></div>
      </section>}
      <div className={styles.moreFooter}>
        <button type="button" onClick={cycleTheme}>{THEME_ICON[theme]}主题：{THEME_LABEL[theme]}</button>
        <button type="button" onClick={() => void logout()}><LogOut size={17} aria-hidden="true" />退出</button>
      </div>
    </Sheet>}

    {palette && <CommandPalette onClose={() => setPalette(false)} />}
  </div>;
}

function RecentList({ people }: { people: RecentPerson[] }) {
  return <ul className={styles.recentList} role="list">
    {people.map((person) => <li key={person.id}>
      <PersonLink href={`/profiles/${person.id}`} title={person.name}>
        <Avatar name={person.name} mediaId={person.avatarMediaId} size={24} round={person.type === "person"} />
        <span>{person.name}</span>
      </PersonLink>
    </li>)}
  </ul>;
}
