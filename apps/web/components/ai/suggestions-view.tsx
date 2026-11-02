"use client";

import { Sparkles } from "lucide-react";
import { useState } from "react";

import { type SuggestionKind, useSuggestionPage } from "../../lib/ai";
import { LoadMore } from "../ui/load-more";
import { PersonLink } from "../ui/person-link";
import { Avatar, EmptyState, Failure, Page, PageHeader, relativeDays, Skeleton } from "../ui/primitives";
import { Segmented } from "../ui/segmented";
import { SuggestionItem } from "./suggestion-item";
import styles from "./suggestions.module.css";

const KIND_LABELS: Record<SuggestionKind, string> = {
  birthday: "生日", residence: "常驻地", hometown: "家乡", company: "公司", job: "职位", school: "学校", email: "邮箱", phone: "电话",
  food: "饮食", interests: "兴趣", family: "家人", meetup: "见面",
};

/** Every pending suggestion, grouped by person, newest evidence first. */
export function SuggestionsView() {
  const [kind, setKind] = useState<SuggestionKind | null>(null);
  const page = useSuggestionPage(kind);
  const data = page.data?.pages[0];
  const people = (page.data?.pages.flatMap((part) => part.people) ?? []).filter((person) => person.items.length);
  const kinds = (Object.keys(KIND_LABELS) as SuggestionKind[]).filter((key) => data?.kinds[key]);
  const all = Object.values(data?.kinds ?? {}).reduce((sum, count) => sum + (count ?? 0), 0);

  return <Page width="narrow">
    <PageHeader title="资料建议" count={all || undefined}
      description="接受后写入资料。" />
    {kinds.length > 1 && <Segmented className={styles.filters} label="按类型筛选" size="sm" value={kind ?? "all"}
      onChange={(next) => { setKind(next === "all" ? null : next); }}
      options={[{ value: "all" as const, label: "全部", count: all },
        ...kinds.map((key) => ({ value: key, label: KIND_LABELS[key], count: data?.kinds[key] }))]} />}
    {page.isPending && <Skeleton height={240} />}
    {page.isError && <Failure onRetry={() => void page.refetch()}>没能读取建议。</Failure>}
    {data && !data.enabled && <EmptyState icon={<Sparkles size={28} aria-hidden="true" />} title="模型功能没有开启" />}
    {data?.enabled && !people.length && <EmptyState icon={<Sparkles size={28} aria-hidden="true" />} title="暂时没有建议">
      新的建议会在每周日凌晨生成。
    </EmptyState>}
    <ul className={styles.people}>
      {people.map((person) => <li key={person.profile_id} className={styles.person}>
        <PersonLink className={styles.who} href={`/profiles/${person.profile_id}`}>
          <Avatar name={person.name} mediaId={person.avatar_media_id} size={36} round />
          <strong>{person.name}</strong>
          <small>{relativeDays(person.generated_at)}生成</small>
        </PersonLink>
        <ul className={styles.list}>
          {person.items.map((item) => <SuggestionItem key={item.fingerprint} profileId={person.profile_id} item={item} />)}
        </ul>
      </li>)}
    </ul>
    {page.hasNextPage && <LoadMore cursor={String(page.data?.pages.length)} busy={page.isFetchingNextPage}
      error={page.isFetchNextPageError} onLoad={() => void page.fetchNextPage()} />}
  </Page>;
}
