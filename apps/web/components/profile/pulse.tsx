"use client";

import type { ProfileOverview, SignificantDate } from "../../lib/api-types";
import { useProfileTab } from "../../lib/queries";
import { Failure, Skeleton, Stat, exactCount, formatCount, relativeDays } from "../ui/primitives";
import { dateTitle } from "./dates";
import styles from "./profile.module.css";
import { Ribbon, type RibbonData, type RibbonMarker } from "./ribbon";

function dateMarkers(dates: SignificantDate[]): RibbonMarker[] {
  return dates.filter((date) => date.kind !== "birthday" && date.year && date.month)
    .map((date) => ({ month: `${date.year}-${String(date.month).padStart(2, "0")}`, kind: "date", label: dateTitle(date) }));
}

const yearMonth = (value: string) => {
  const date = new Date(value);
  return `${date.getFullYear()}年${date.getMonth() + 1}月`;
};

/** Headline numbers and the full-history ribbon. Each part is left out when the API has nothing for it. */
export function RelationshipPulse({ profileId, overview, dates, groupCount }: {
  profileId: string; overview?: Pick<ProfileOverview, "messages" | "contact_events">; dates: SignificantDate[]; groupCount: number;
}) {
  const messages = overview?.messages;
  const calls = overview?.contact_events?.calls;
  const direct = !!messages?.total;
  const months = useProfileTab<RibbonData>(profileId, "ribbon", undefined, direct);
  if (!messages && !calls?.total && !groupCount) return null;
  const lastInGroup = !!messages?.last_group_at && messages.last_group_at === messages.last_contact_at;
  return <section className={styles.pulse} aria-label="往来概况">
    <div className={styles.pulseStats}>
      {direct && <Stat value={formatCount(messages.total)} title={exactCount(messages.total)} label="条私聊" />}
      {messages && <Stat value={relativeDays(messages.last_contact_at)} label={lastInGroup ? "最近联系（群里）" : "最近联系"} />}
      {!!calls?.total && <Stat value={formatCount(calls.total)} title={exactCount(calls.total)} label="次通话" />}
      {groupCount > 0 && <Stat value={groupCount} label="个共同群" />}
      {!!messages?.group_total && <Stat value={formatCount(messages.group_total)} title={exactCount(messages.group_total)} label="条群里发言" />}
      {messages?.first_contact_at && <Stat size="sm" value={yearMonth(messages.first_contact_at)} label="首次联系" />}
    </div>
    {direct && <div className={styles.ribbonFrame}>
      {months.isError ? <Failure onRetry={() => void months.refetch()}>月度统计读取失败。</Failure>
        : !months.data ? <Skeleton height={128} />
        : <>
          <Ribbon profileId={profileId} data={{ timezone: months.data.timezone, months: months.data.months, markers: dateMarkers(dates) }} />
          <p className={styles.legend}>
            <span data-series="sent">我发的</span><span data-series="received">对方发的</span><span data-series="peak">高峰月</span>
            {months.data.months.some((month) => month.calls) && <span data-series="call">通话</span>}
            {months.data.months.some((month) => month.interactions) && <span data-series="interaction" title="对方在朋友圈的点赞和评论">朋友圈互动</span>}
          </p>
        </>}
    </div>}
  </section>;
}
