"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";

import type { HomePerson, OrganizationCount } from "../../lib/api-types";
import { localMediaUrl } from "../../lib/media";
import { useInsights } from "../../lib/queries";
import { dateTitle } from "../profile/dates";
import { SocialAnalytics } from "../social/social-analytics";
import { Button } from "../ui/button";
import { Avatar, exactCount, formatCount, formatNumber, Page, PageHeader, PulseStrip, relativeDays, Section, Skeleton, SourceMark } from "../ui/primitives";
import { Segmented } from "../ui/segmented";
import { ContactCalendar } from "./contact-calendar";
import styles from "./insights.module.css";

const THRESHOLDS = [90, 180, 365] as const;
type Threshold = (typeof THRESHOLDS)[number];
const DORMANT_PREVIEW = 12;
const POSTERS_PREVIEW = 10;

function recentLine(person: HomePerson) {
  const inGroup = !!person.last_group_at && person.last_group_at === person.last_contact_at;
  const count = person.direct_message_count
    ? `私聊 ${formatCount(person.direct_message_count)} 条`
    : `群里发言 ${formatCount(person.group_message_count)} 条`;
  return `${relativeDays(person.last_contact_at)}${inGroup ? "在群里" : ""}，${count}`;
}

export function InsightsView() {
  const params = useSearchParams();
  const router = useRouter();
  const requested = Number(params.get("dormant"));
  const days: Threshold = (THRESHOLDS as readonly number[]).includes(requested) ? requested as Threshold : 90;
  const [expanded, setExpanded] = useState(false);
  const [showPosters, setShowPosters] = useState(false);
  const insights = useInsights(days);
  const data = insights.data;
  const dormant = data?.dormant_contacts ?? [];

  return <Page width="wide" label="洞察">
    <PageHeader title="洞察" />
    <Section title="联系日历" className={styles.panel}><ContactCalendar /></Section>
    {!data ? <Skeleton height={480} /> : <>
      <div className={styles.columns}>
        <Section title="很久没联系" className={styles.panel} action={<Segmented size="sm" label="多久没联系" value={String(days)}
          onChange={(value) => { setExpanded(false); router.replace(`/insights?dormant=${value}`, { scroll: false }); }}
          options={THRESHOLDS.map((value) => ({ value: String(value), label: value === 365 ? "1 年" : `${value} 天` }))} />}>
          {dormant.length ? <>
            <ul className={styles.people}>{dormant.slice(0, expanded ? undefined : DORMANT_PREVIEW).map((person) => <li key={person.profile_id}><Link href={`/profiles/${person.profile_id}`}>
              <Avatar name={person.name} mediaId={person.avatar_media_id} size={36} />
              <span><strong>{person.name}</strong><small>上次联系 {relativeDays(person.last_contact_at)}</small></span>
              <b title={exactCount(person.direct_message_count)}>{formatCount(person.direct_message_count)}<small>条{person.calls ? `，${person.calls} 次通话` : ""}</small></b>
            </Link></li>)}</ul>
            {dormant.length > DORMANT_PREVIEW && <Button size="sm" variant="ghost" onClick={() => setExpanded(!expanded)}>
              {expanded ? "收起" : `显示全部 ${dormant.length} 人`}
            </Button>}
          </> : <p className={styles.none}>超过 {days} 天没联系的人里，没有聊得多的人。</p>}
        </Section>

        {(data.upcoming_dates || data.recent_dates) && <Section title="重要日子" className={styles.panel}>
          {data.upcoming_dates && <ul className={styles.people}>{data.upcoming_dates.map((item) => <li key={`${item.profile_id}-${item.kind}-${item.label ?? ""}-${item.date}`}><Link href={`/profiles/${item.profile_id}`}>
            <Avatar name={item.name} mediaId={item.avatar_media_id} size={36} />
            <span><strong>{item.name}</strong><small>{dateTitle(item)} · {formatDay(item.date)}{item.years ? `，${item.years} ${item.kind === "birthday" ? "岁" : "周年"}` : ""}</small></span>
            <b data-today={!item.days_until ? "" : undefined}>{item.days_until ? `${item.days_until} 天后` : "今天"}</b>
          </Link></li>)}</ul>}
          {data.recent_dates && <>
            <h3 className={styles.subhead}>刚过去的</h3>
            <ul className={styles.people}>{data.recent_dates.map((item) => <li key={`${item.profile_id}-${item.kind}-${item.label ?? ""}-${item.date}`}><Link href={`/profiles/${item.profile_id}`}>
              <Avatar name={item.name} mediaId={item.avatar_media_id} size={36} />
              <span><strong>{item.name}</strong><small>{dateTitle(item)} · {formatDay(item.date)}</small></span>
              <b>{item.days_ago} 天前</b>
            </Link></li>)}</ul>
          </>}
        </Section>}

        {data.recent_posters && <Section title="最近发动态的人" className={styles.panel}>
          <ul className={styles.posters}>{data.recent_posters.slice(0, showPosters ? undefined : POSTERS_PREVIEW).map((person) => <li key={person.profile_id}>
            <Link href={`/profiles/${person.profile_id}?tab=timeline`}>
              <Avatar name={person.name} mediaId={person.avatar_media_id} size={36} />
              <span className={styles.posterMeta}>
                <strong>{person.name}</strong>
                <small>{[person.posts && `${person.posts} 条动态`, person.stories && `${person.stories} 个快拍`].filter(Boolean).join("，")}，{relativeDays(person.latest_at)}</small>
                <span className={styles.sources}>{person.providers.map((provider) => <SourceMark key={provider} provider={provider} size={11} />)}</span>
              </span>
              {!!person.thumbnail_media_ids?.length && <span className={styles.thumbs}>
                {person.thumbnail_media_ids.map((id) => <img key={id} src={localMediaUrl(id)} alt="" width={44} height={44} loading="lazy" />)}
              </span>}
            </Link>
          </li>)}</ul>
          {data.recent_posters.length > POSTERS_PREVIEW && <Button size="sm" variant="ghost" onClick={() => setShowPosters(!showPosters)}>
            {showPosters ? "收起" : `显示全部 ${data.recent_posters.length} 人`}
          </Button>}
        </Section>}
      </div>

      {data.recent_contacts && <Section title="最近联系" className={styles.panel}>
        <ul className={styles.recent}>{data.recent_contacts.map((person) => <li key={person.profile_id}><Link href={`/profiles/${person.profile_id}`}>
          <Avatar name={person.name} mediaId={person.avatar_media_id} size={36} />
          <span><strong>{person.name}</strong><small>{recentLine(person)}</small></span>
          {person.months && <PulseStrip months={person.months} compact label={`${person.name} 近 12 个月聊天`} />}
        </Link></li>)}</ul>
      </Section>}

      {(data.top_companies || data.top_schools) && <div className={styles.columns}>
        <Ranking title="朋友在哪里工作" rows={data.top_companies} filter="company" />
        <Ranking title="大家的学校" rows={data.top_schools} filter="school" />
      </div>}
    </>}
    <SocialAnalytics />
  </Page>;
}

function Ranking({ title, rows, filter }: { title: string; rows?: OrganizationCount[]; filter: "company" | "school" }) {
  if (!rows) return null;
  const top = Math.max(...rows.map((row) => row.people), 1);
  return <Section title={title} className={styles.panel}><ol className={styles.ranking}>
    {rows.map((row) => <li key={row.id}><Link href={`/profiles?${filter}=${encodeURIComponent(row.id)}`}>
      <span>{row.name}</span><b>{row.people}</b><i style={{ inlineSize: `${(row.people / top) * 100}%` }} />
    </Link></li>)}
  </ol></Section>;
}

function formatDay(value: string) {
  const date = new Date(`${value}T00:00:00`);
  return `${date.getMonth() + 1} 月 ${date.getDate()} 日`;
}
