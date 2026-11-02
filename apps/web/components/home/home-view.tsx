"use client";

import { Cake, CalendarDays, Heart, MessageCircleHeart } from "lucide-react";
import Link from "next/link";

import { useReconnect } from "../../lib/ai";
import type { DateKind, HomePerson, InsightsSummary } from "../../lib/api-types";
import { useHome, useInsights } from "../../lib/queries";
import { DigestCard } from "../digest/digest-card";
import { SocialFeed } from "../feed/social-feed";
import { dateTitle } from "../profile/dates";
import { PersonLink } from "../ui/person-link";
import { Avatar, Faint, formatCount, formatNumber, Page, PulseStrip, relativeDays, Skeleton } from "../ui/primitives";
import styles from "./home.module.css";
import { ReconnectSuggestions } from "./reconnect-suggestions";
import { WeeklyHighlights } from "./weekly-highlights";

const SOON_DAYS = 14;
const RECONNECT = 3;
const DATE_ICON: Record<DateKind, typeof Cake> = { birthday: Cake, anniversary: Heart, other: CalendarDays };
const today = new Intl.DateTimeFormat("zh-CN", { month: "long", day: "numeric", weekday: "long" });

type Upcoming = NonNullable<InsightsSummary["upcoming_dates"]>[number];

function greeting() {
  const hour = new Date().getHours();
  return hour < 5 ? "夜深了" : hour < 11 ? "早上好" : hour < 14 ? "中午好" : hour < 18 ? "下午好" : "晚上好";
}

function formatDay(value: string) {
  const date = new Date(`${value}T00:00:00`);
  return `${date.getMonth() + 1}月${date.getDate()}日`;
}

function DateCard({ item }: { item: Upcoming }) {
  const Icon = DATE_ICON[item.kind];
  const days = item.days_until ?? 0;
  return <PersonLink className={styles.card} href={`/profiles/${item.profile_id}`} data-today={days === 0 ? "" : undefined}>
    <Avatar name={item.name} mediaId={item.avatar_media_id} size={44} round />
    <span className={styles.cardText}>
      <strong>{item.name}</strong>
      <small><Icon size={13} aria-hidden="true" />{dateTitle(item)} · {formatDay(item.date)}{item.years ? ` · ${item.years} ${item.kind === "birthday" ? "岁" : "周年"}` : ""}</small>
    </span>
    <b className={styles.when}>{days === 0 ? "今天" : days === 1 ? "明天" : `${days} 天后`}</b>
  </PersonLink>;
}

/** Someone worth a message. `reason` is the model's one-line why, once that runs; otherwise the history speaks. */
export function ReconnectCard({ person, reason }: { person: HomePerson; reason?: string }) {
  return <PersonLink className={styles.card} href={`/profiles/${person.profile_id}`}>
    <Avatar name={person.name} mediaId={person.avatar_media_id} size={44} round />
    <span className={styles.cardText}>
      <strong>{person.name}</strong>
      <small>{reason ?? `上次联系 ${relativeDays(person.last_contact_at)} · 以前聊过 ${formatCount(person.direct_message_count)} 条`}</small>
    </span>
    {person.months && <span className={styles.cardPulse}><PulseStrip months={person.months} compact label={`${person.name} 近 12 个月聊天`} /></span>}
  </PersonLink>;
}

function Remember() {
  const insights = useInsights(90);
  const suggestions = useReconnect();
  const data = insights.data;
  if (insights.isError) return null;
  if (!data || suggestions.isPending) return <div className={styles.rememberGrid}><Skeleton height={220} /><Skeleton height={220} /></div>;
  const dates = (data.upcoming_dates ?? []).filter((item) => (item.days_until ?? 0) <= SOON_DAYS).slice(0, 3);
  // With the model on, its suggestions (which can be acted on) replace the plain dormant list.
  const suggested = suggestions.data?.enabled ? suggestions.data.people : null;
  const reconnect = suggested ? [] : (data.dormant_contacts ?? []).slice(0, RECONNECT);
  if (!dates.length && !reconnect.length && !suggested?.length) return null;
  return <section className={styles.remember} aria-labelledby="remember-title">
    <h2 id="remember-title">今天要记得</h2>
    <div className={styles.rememberGrid}>
      {dates.length > 0 && <div className={styles.column}>
        <h3><CalendarDays size={14} aria-hidden="true" />两周内的日子</h3>
        <div className={styles.cards}>{dates.map((item) =>
          <DateCard key={`${item.profile_id}-${item.kind}-${item.label ?? ""}-${item.date}`} item={item} />)}</div>
      </div>}
      {reconnect.length > 0 && <div className={styles.column}>
        <h3><MessageCircleHeart size={14} aria-hidden="true" />该联系了<Link href="/insights">全部</Link></h3>
        <div className={styles.cards}>{reconnect.map((person) => <ReconnectCard key={person.profile_id} person={person} />)}</div>
      </div>}
      {suggested && suggested.length > 0 && <ReconnectSuggestions people={suggested} shown={2}
        onRedraw={() => void suggestions.refetch()} redrawing={suggestions.isFetching} />}
    </div>
  </section>;
}

export function HomeView() {
  const home = useHome();
  const numbers = home.data?.key_numbers;
  return <Page className={styles.page}>
    <header className={styles.greeting}>
      <p>{today.format(new Date())}</p>
      <h1>{greeting()}</h1>
      {numbers ? <p className={styles.numbers}>
        <Link href="/profiles?type=person"><b>{formatNumber(numbers.contacts)}</b> 位联系人</Link>
        <Link href="/insights"><b>{formatNumber(numbers.active_contacts_30d)}</b> 位 30 天内聊过</Link>
        <Link href="/messages"><b>{formatNumber(numbers.messages_30d)}</b> 条近 30 天消息</Link>
        <Link href="/profiles?type=group"><b>{formatNumber(numbers.groups)}</b> 个群</Link>
      </p> : home.isError ? <Faint>暂时读不到概况。</Faint> : <Skeleton height={20} style={{ inlineSize: 420, maxInlineSize: "100%" }} />}
    </header>
    <DigestCard />
    <WeeklyHighlights />
    <Remember />
    <section className={styles.feed}><SocialFeed preview={6} title="最近动态" /></section>
  </Page>;
}
