"use client";

import { useQueryClient } from "@tanstack/react-query";
import { Users } from "lucide-react";

import { queryKeys, useProfileTab } from "../../lib/queries";
import { MomentsCircle } from "../moments-circle";
import { RelationshipEditor } from "../relationship-editor";
import { ButtonLink } from "../ui/button";
import { PersonLink } from "../ui/person-link";
import { Avatar, EmptyState, Panel } from "../ui/primitives";
import { groupRelationships, relationshipLabel } from "./fields";
import styles from "./profile.module.css";
import type { Relationship } from "./types";

const DIRECTION: Record<string, string> = { both: "双向关系", incoming: "对方 → 本人", outgoing: "本人 → 对方" };

export function RelationshipsTab({ profileId }: { profileId: string }) {
  const client = useQueryClient();
  const relationships = useProfileTab<Relationship[]>(profileId, "relationships");
  const grouped = groupRelationships(relationships.data ?? []);
  const refresh = () => void client.invalidateQueries({ queryKey: queryKeys.profileTab(profileId, "relationships") });
  return <div className={styles.relationshipsTab}>
    <MomentsCircle profileId={profileId} />
    <Panel className={styles.tabPanel}>
      <header className={styles.tabHeader}>
        <h2>关系类型</h2>
        <ButtonLink size="sm" href={`/relationships?focus=${profileId}`}>在关系图中查看</ButtonLink>
      </header>
      <RelationshipEditor profileId={profileId} onSaved={refresh} />
      {grouped.length ? <div className={styles.relationGroups}>{grouped.map(([type, entries]) => <div key={type}>
        <h3>{relationshipLabel(type)} <small>{entries.length} 条</small></h3>
        {entries.map((item) => <PersonLink className={styles.personRow} href={`/profiles/${item.related_profile_id}`} key={item.id}>
          <Avatar name={item.related_display_name} size={28} round={item.related_profile_type === "person"} />
          <span>{item.related_display_name}</span>
          <small>{DIRECTION[item.direction] ?? DIRECTION.outgoing} · {item.source_type}</small>
        </PersonLink>)}
      </div>)}</div> : relationships.isSuccess && <EmptyState icon={<Users size={20} />} title="暂无关系">导入关系或手工添加后，这里会显示真实数据。</EmptyState>}
    </Panel>
  </div>;
}
