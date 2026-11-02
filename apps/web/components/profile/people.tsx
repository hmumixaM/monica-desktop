"use client";

import { ChevronRight, Link2, MessageCircle, ShieldCheck, UsersRound } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { useProfileTab } from "../../lib/queries";
import { Button, ButtonLink } from "../ui/button";
import { LoadMore } from "../ui/load-more";
import { PersonLink } from "../ui/person-link";
import { Avatar, Failure, Faint, formatNumber, Panel, Section, Stat } from "../ui/primitives";
import { formatDate, groupRelationships, relationshipLabel, roleLabel } from "./fields";
import styles from "./profile.module.css";
import type { GroupLink, Profile, Relationship } from "./types";

const RELATION_TYPES = 8;
const PER_TYPE = 5;
const GROUPS = 12;

function PersonRow({ href, name, mediaId, meta, round = true }: { href: string; name: string; mediaId?: string | null; meta?: string; round?: boolean }) {
  return <PersonLink className={styles.personRow} href={href}>
    <Avatar name={name} mediaId={mediaId} size={28} round={round} />
    <span>{name}</span>
    {meta && <small>{meta}</small>}
    <ChevronRight size={13} aria-hidden="true" />
  </PersonLink>;
}

/** Relationship edges grouped by type; group memberships are shown under 所属群 instead. */
export function RelationshipsBlock({ profileId }: { profileId: string }) {
  const relationships = useProfileTab<Relationship[]>(profileId, "relationships");
  const grouped = groupRelationships((relationships.data ?? []).filter((item) => !item.id.startsWith("membership:")));
  if (!grouped.length) return null;
  return <Section title="关系" icon={<Link2 size={15} aria-hidden="true" />}
    action={<Link href={`/relationships?focus=${profileId}`}>在关系图中查看</Link>}>
    <div className={styles.relationGroups}>
      {grouped.slice(0, RELATION_TYPES).map(([type, entries]) => <div key={type}>
        <h3>{relationshipLabel(type)} <small>{entries.length}</small></h3>
        {entries.slice(0, PER_TYPE).map((item) => <PersonRow key={item.id} href={`/profiles/${item.related_profile_id}`} name={item.related_display_name}
          round={item.related_profile_type === "person"} />)}
        {entries.length > PER_TYPE && <Faint>还有 {entries.length - PER_TYPE} 条关系</Faint>}
      </div>)}
    </div>
  </Section>;
}

export function GroupsBlock({ profile }: { profile: Profile }) {
  const [all, setAll] = useState(false);
  const groups = profile.member_of_groups ?? [];
  if (!groups.length) return null;
  return <Section title="所属微信群" icon={<UsersRound size={15} aria-hidden="true" />} action={<small>{profile.linked_group_count ?? groups.length}</small>}>
    <div className={styles.people}>
      {groups.slice(0, all ? groups.length : GROUPS).map((group) => <PersonRow key={group.group_profile_id} href={`/profiles/${group.group_profile_id}`}
        name={group.display_name} mediaId={group.avatar_media_id} meta={roleLabel(group.role)} round={false} />)}
    </div>
    {groups.length > GROUPS && <Button variant="ghost" size="sm" onClick={() => setAll((value) => !value)}>{all ? "收起群列表" : `查看全部 ${groups.length} 个群`}</Button>}
  </Section>;
}

export function OwnersBlock({ profile }: { profile: Profile }) {
  const owners = profile.group_owners ?? [];
  return <Section title="群主" icon={<ShieldCheck size={15} aria-hidden="true" />} action={<small>{owners.length}</small>}>
    {owners.length
      ? <div className={styles.people}>{owners.slice(0, 8).map((owner) => <PersonRow key={owner.person_profile_id} href={`/profiles/${owner.person_profile_id}`}
        name={owner.display_name} mediaId={owner.avatar_media_id} meta="群主" />)}</div>
      : <Faint>当前导入没有明确的群主关系</Faint>}
  </Section>;
}

/** The group page body: chat totals and a paged member grid. */
export function GroupOverview({ profile }: { profile: Profile }) {
  const [members, setMembers] = useState<GroupLink[]>(profile.group_members || []);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function more() {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      const response = await fetch(`/api/v1/groups/${profile.id}/members?offset=${members.length}`);
      if (!response.ok) throw new Error(String(response.status));
      const next: GroupLink[] = await response.json();
      setMembers((old) => [...old, ...next.filter((row) => !old.some((member) => member.person_profile_id === row.person_profile_id))]);
    } catch {
      setError("成员读取失败，请重试。");
    } finally {
      setBusy(false);
    }
  }
  const reported = profile.presentation?.sources.wechat?.member_count;
  const stats = profile.group_summary;
  return <Panel className={styles.tabPanel}>
    <header className={styles.tabHeader}>
      <div><h2>群成员</h2><Faint>{profile.group_member_count} 位已关联成员{reported != null ? ` · 微信记录 ${String(reported)} 位` : ""}</Faint></div>
      <ButtonLink href={`/profiles/${profile.id}?tab=messages`}><MessageCircle size={15} />打开群聊</ButtonLink>
    </header>
    {stats && <div className={styles.statRow}>
      <Stat value={formatNumber(stats.messages)} label="条群聊消息" />
      <Stat value={stats.active_senders} label="位发言者" />
      {stats.last && <Stat size="sm" value={formatDate(stats.last)} label="最近发言" />}
    </div>}
    <div className={styles.memberGrid}>
      {members.map((member) => <PersonLink href={`/profiles/${member.person_profile_id}`} key={member.person_profile_id}>
        <Avatar name={member.display_name} mediaId={member.avatar_media_id} size={40} round />
        <span><strong>{member.display_name}</strong><small>{roleLabel(member.role)}</small></span>
      </PersonLink>)}
    </div>
    {error && <Failure>{error}</Failure>}
    {members.length < (profile.group_member_count || 0) && <LoadMore cursor={String(members.length)} busy={busy} error={!!error} onLoad={() => void more()} label="加载更多成员" />}
  </Panel>;
}
