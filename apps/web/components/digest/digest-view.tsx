"use client";

import { Baby, Briefcase, Cake, CalendarDays, GraduationCap, Heart, HeartHandshake, House, Plane, RefreshCw, Sparkles, UserPlus } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import type { CSSProperties, ReactNode } from "react";

import { type Digest, type DigestPerson, type LifeEventKind, useDigest, useDigests, useRunAiJob, useWeeklyPeople } from "../../lib/ai";
import { Week } from "../ai/profile-ai";
import { dateTitle } from "../profile/dates";
import { Button } from "../ui/button";
import { LoadMore } from "../ui/load-more";
import { Select } from "../ui/field";
import { PersonLink } from "../ui/person-link";
import { Avatar, EmptyState, Failure, formatCount, formatDateTime, formatNumber, Page, PageHeader, relativeDays, Section, Skeleton, SourceMark } from "../ui/primitives";
import { useToast } from "../ui/toast";
import styles from "./digest.module.css";
import { shortDay, weekRange } from "./week";

const EVENT_ICON: Record<LifeEventKind, typeof Plane> = {
  new_job: Briefcase, move: House, travel: Plane, wedding: HeartHandshake, graduation: GraduationCap, new_baby: Baby, other: Sparkles,
};
const EVENT_LABEL: Record<LifeEventKind, string> = {
  new_job: "新工作", move: "搬家", travel: "旅行", wedding: "婚礼", graduation: "毕业", new_baby: "添丁", other: "近况",
};
const DATE_ICON = { birthday: Cake, anniversary: Heart, other: CalendarDays };

function Who({ person, children }: { person: DigestPerson; children?: ReactNode }) {
  return <PersonLink className={styles.who} href={`/profiles/${person.profile_id}`}>
    <Avatar name={person.name} mediaId={person.avatar_media_id} size={32} round />
    <span><strong>{person.name}</strong>{children && <small>{children}</small>}</span>
  </PersonLink>;
}

function person(digest: Digest, id: string): DigestPerson {
  const known = digest.people[id];
  return { profile_id: id, name: known?.name ?? "未知联系人", avatar_media_id: known?.avatar_media_id };
}

function Story({ digest }: { digest: Digest }) {
  const { narrative, facts } = digest;
  const top = facts.top_contacts[0]?.messages || 1;
  return <article className={styles.story}>
    {narrative && <p className={styles.summary}>{narrative.summary}</p>}
    <p className={styles.totals}>
      <span><b>{formatNumber(facts.totals.messages)}</b> 条消息</span>
      <span>和 <b>{formatNumber(facts.totals.people)}</b> 人私聊</span>
      <span><b>{formatNumber(facts.totals.posts)}</b> 条联系人动态</span>
    </p>

    <Section title="生活里的大事" icon={<Sparkles size={15} aria-hidden="true" />}>
      {narrative && narrative.life_events.length > 0 && <ul className={styles.events}>{narrative.life_events.map((event) => {
        const Icon = EVENT_ICON[event.kind];
        const date = digest.post_dates[event.post_id];
        return <li key={`${event.post_id}-${event.kind}`}>
          <span className={styles.eventIcon} data-kind={event.kind}><Icon size={16} aria-hidden="true" /></span>
          <div>
            <p><strong>{person(digest, event.profile_id).name}</strong> · {EVENT_LABEL[event.kind]}</p>
            <p>{event.text}</p>
            <Link href={`/timeline?event=${event.post_id}`}>看动态{date ? ` · ${shortDay(date)}` : ""}</Link>
          </div>
        </li>;
      })}</ul>}
    </Section>

    <Section title="这周可以联系" icon={<HeartHandshake size={15} aria-hidden="true" />}>
      {narrative && narrative.suggestions.length > 0 && <ul className={styles.people}>{narrative.suggestions.map((item) =>
        <li key={item.profile_id}><Who person={person(digest, item.profile_id)}>{item.reason}</Who></li>)}</ul>}
    </Section>

    <Section title="上周联系最多">
      {facts.top_contacts.length > 0 && <ol className={styles.ranking}>{facts.top_contacts.map((row) => <li key={row.profile_id}>
        <Who person={row} />
        <span className={styles.bar} style={{ "--share": row.messages / top } as CSSProperties} aria-hidden="true" />
        <small>{formatCount(row.messages)} 条{row.calls ? ` · ${row.calls} 次通话` : ""}</small>
      </li>)}</ol>}
    </Section>

    <Section title="接下来一周的日子">
      {facts.upcoming_dates.length > 0 && <ul className={styles.people}>{facts.upcoming_dates.map((row) => {
        const Icon = DATE_ICON[row.kind];
        return <li key={`${row.profile_id}-${row.kind}-${row.date}`}>
          <Who person={row}><Icon size={12} aria-hidden="true" />{dateTitle(row)} · {shortDay(row.date)}{row.years ? ` · ${row.years} ${row.kind === "birthday" ? "岁" : "周年"}` : ""}</Who>
        </li>;
      })}</ul>}
    </Section>

    <Section title="新认识与新关联" icon={<UserPlus size={15} aria-hidden="true" />}>
      {(facts.new_people.length > 0 || facts.new_links.length > 0) && <div className={styles.chips}>
        {facts.new_people.map((row) => <PersonLink key={row.profile_id} href={`/profiles/${row.profile_id}`} className={styles.chip}>{row.name}<small>新联系人</small></PersonLink>)}
        {facts.new_links.map((row) => <PersonLink key={`${row.profile_id}-${row.provider}`} href={`/profiles/${row.profile_id}`} className={styles.chip}>
          <SourceMark provider={row.provider} size={11} />{row.name}
        </PersonLink>)}
      </div>}
    </Section>

    <Section title="刚刚变得疏远">
      {facts.newly_dormant.length > 0 && <ul className={styles.people}>{facts.newly_dormant.map((row) =>
        <li key={row.profile_id}><Who person={row}>上次联系 {relativeDays(row.last_contact_at)} · 一共聊过 {formatCount(row.messages)} 条</Who></li>)}</ul>}
    </Section>

    <p className={styles.colophon}>{digest.model ? `由 ${digest.model} 根据资料、动态文字和聊天数量撰写，不读取聊天内容。` : "文字回顾尚未生成。"}
      {" "}更新于 {formatDateTime(digest.generated_at, true)}</p>
  </article>;
}

/** 每周回顾: what happened last week, and who to reach out to. Past weeks stay readable. */
export function DigestView() {
  const params = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const toast = useToast();
  const list = useDigests();
  const run = useRunAiJob();
  const digests = list.data?.digests ?? [];
  const week = params.get("week") ?? undefined;
  const people = useWeeklyPeople(week);
  const peoplePage = people.data?.pages[0];
  const selectedWeek = week ?? peoplePage?.week_start ?? digests[0]?.week_start;
  const weeks = [...new Set([...digests.map((item) => item.week_start), ...(peoplePage?.weeks ?? [])])].sort().reverse();
  const showingPeople = params.get("view") === "people";
  const viewHref = (view: string) => {
    const next = new URLSearchParams(params.toString());
    next.set("view", view);
    if (selectedWeek) next.set("week", selectedWeek);
    return `${pathname}?${next}`;
  };
  const digest = useDigest(list.data?.enabled ? selectedWeek : undefined);
  const choose = (value: string) => {
    const next = new URLSearchParams(params.toString());
    next.set("week", value);
    router.replace(`${pathname}?${next}`, { scroll: false });
  };
  const regenerate = () => run.mutate("ai.digest", {
    onSuccess: () => toast.show({ message: "已排队重新生成，几分钟后刷新查看" }),
    onError: () => toast.show({ message: "没能排队，请到设置里查看模型服务状态" }),
  });

  if (list.isError && !showingPeople) return <Page><Failure onRetry={() => void list.refetch()}>暂时读不到每周回顾。</Failure></Page>;
  if (!list.data && !showingPeople) return <Page><Skeleton height={480} /></Page>;
  if (list.data?.enabled === false || peoplePage?.enabled === false) return <Page><EmptyState title="模型功能未开启">在 NAS 的 .env 里设置 AI_ENABLED=true 后，每周一早上 8 点会生成上周回顾。</EmptyState></Page>;

  return <Page className={styles.page}>
    <PageHeader title="每周回顾" description={selectedWeek ? weekRange(selectedWeek) : "每周一早上 8 点生成"} actions={<>
      {weeks.length > 1 && <Select aria-label="选择周" value={selectedWeek ?? ""} onChange={(event) => choose(event.target.value)}>
        {weeks.map((value) => <option key={value} value={value}>{weekRange(value)}</option>)}
      </Select>}
      {!showingPeople && <Button size="sm" variant="ghost" onClick={regenerate} disabled={run.isPending}><RefreshCw size={14} />重新生成</Button>}
    </>} />
    <nav className={styles.views} aria-label="回顾内容">
      <Link href={viewHref("overview")} aria-current={!showingPeople ? "page" : undefined}>整体回顾</Link>
      <Link href={viewHref("people")} aria-current={showingPeople ? "page" : undefined}>个人近况{peoplePage ? ` · ${peoplePage.total} 人` : ""}</Link>
    </nav>
    {showingPeople ? people.isError && !peoplePage ? <Failure onRetry={() => void people.refetch()}>个人近况读取失败。</Failure>
      : !peoplePage ? <Skeleton height={300} />
        : !peoplePage.total ? <EmptyState title="这一周还没有个人近况">已生成的个人近况会在这里汇总，也可以切换周查看以前的记录。</EmptyState>
          : <div className={styles.story}>
            <p className={styles.totals}>已整理 {formatNumber(peoplePage.total)} 人的近况</p>
            <ul className={styles.weeklyPeople}>{people.data?.pages.flatMap((page) => page.people).map((person) =>
              <li key={person.profile_id}>
                <Who person={person} />
                <Week item={person} profileId={person.profile_id} showWeek={false} />
              </li>)}</ul>
            {people.hasNextPage && <LoadMore cursor={people.data?.pages.at(-1)?.next_cursor ?? ""} busy={people.isFetchingNextPage}
              error={people.isFetchNextPageError} onLoad={() => void people.fetchNextPage()} />}
          </div>
      : !digests.some((item) => item.week_start === selectedWeek) ? <EmptyState title="这一周还没有整体回顾">可以切换周或查看个人近况。“重新生成”会整理最近完整一周的整体回顾。</EmptyState>
      : digest.isError ? <Failure onRetry={() => void digest.refetch()}>这一周的回顾读取失败。</Failure>
        : digest.data ? <Story digest={digest.data} /> : <Skeleton height={480} />}
  </Page>;
}
