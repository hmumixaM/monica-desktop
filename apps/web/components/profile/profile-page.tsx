"use client";

import { useQueryClient } from "@tanstack/react-query";
import { UserX } from "lucide-react";
import dynamic from "next/dynamic";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";

import { queryKeys, useProfile, useProfileSummary } from "../../lib/queries";
import { rememberPerson } from "../../lib/recent";
import { transitionTo } from "../../lib/transitions";
import { Button, ButtonLink } from "../ui/button";
import { PhotoViewer } from "../ui/photo-viewer";
import { EmptyState, Page, Skeleton } from "../ui/primitives";
import { Tabs } from "../ui/tabs";
import { ProfileHero, type Photo } from "./hero";
import { ProfileOverview } from "./overview";
import { GroupOverview } from "./people";
import styles from "./profile.module.css";
import { RelationshipPulse } from "./pulse";
import type { Profile } from "./types";

const ProfilePhotoGallery = dynamic(() => import("../faces/profile-photos").then(module => module.ProfilePhotoGallery), { loading: () => <Skeleton height={160} /> });
const SocialFeed = dynamic(() => import("../feed/social-feed").then(module => module.SocialFeed), { loading: () => <Skeleton height={160} /> });
const MessagesTab = dynamic(() => import("./chat-detail").then(module => module.MessagesTab), { loading: () => <Skeleton height={160} /> });
const ContactEvents = dynamic(() => import("./contact-events").then(module => module.ContactEvents), { loading: () => <Skeleton height={160} /> });
const FieldEditor = dynamic(() => import("./field-editor").then(module => module.FieldEditor), { loading: () => <Skeleton height={160} /> });
const HistoryView = dynamic(() => import("./history").then(module => module.HistoryView), { loading: () => <Skeleton height={160} /> });
const RecordsView = dynamic(() => import("./records").then(module => module.RecordsView), { loading: () => <Skeleton height={160} /> });
const RelationshipsTab = dynamic(() => import("./relationships-tab").then(module => module.RelationshipsTab), { loading: () => <Skeleton height={160} /> });

function tabsFor(person: boolean) {
  return [
    { id: "overview", label: person ? "概览" : "群资料" },
    ...(person ? [{ id: "timeline", label: "动态" }, { id: "photos", label: "照片" }] : []),
    { id: "messages", label: "聊天" },
    { id: "records", label: "笔记与记录" },
    { id: "contact-events", label: "通话与收支" },
    { id: "relationships", label: person ? "关系" : "成员" },
    { id: "history", label: "历史" },
  ];
}

export function ProfileView({ profileId }: { profileId: string }) {
  const params = useSearchParams();
  const router = useRouter();
  const client = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [photo, setPhoto] = useState<Photo | null>(null);
  const summary = useProfileSummary(profileId);
  const canonicalId = summary.data?.profile.id ?? profileId;
  const detail = useProfile(canonicalId, !!summary.data);
  const overview = { data: summary.data?.overview };
  const profile = (detail.data as Profile | undefined) ?? summary.data?.profile;
  useEffect(() => {
    if (canonicalId !== profileId) router.replace(`/profiles/${canonicalId}${params.size ? `?${params}` : ""}`);
  }, [canonicalId, profileId, router, params]);

  useEffect(() => {
    if (profile) rememberPerson({ id: profile.id, name: profile.display_name, avatarMediaId: profile.avatar_media_id ?? null, type: profile.profile_type });
  }, [profile?.id, profile?.display_name, profile?.avatar_media_id, profile?.profile_type]);

  if (summary.isError) return <Page width="narrow"><EmptyState icon={<UserX size={28} />} title="找不到这份资料"
    actions={<><Button variant="primary" onClick={() => void summary.refetch()}>重试</Button><ButtonLink href="/profiles">返回联系人</ButtonLink></>}>
    它可能已被合并或删除。</EmptyState></Page>;
  if (!profile) return <Page className={styles.page} label="正在读取资料">
    <div className={styles.loadingHero}><Skeleton height={104} style={{ inlineSize: 104, borderRadius: "50%" }} /><div><Skeleton height={34} style={{ inlineSize: "40%" }} /><Skeleton height={16} style={{ inlineSize: "65%" }} /></div></div>
    <Skeleton height={168} />
    <Skeleton height={420} />
  </Page>;

  const person = profile.profile_type === "person";
  const tabs = tabsFor(person);
  const requested = params.get("tab") || "overview";
  const tab = tabs.some((item) => item.id === requested) ? requested : "overview";
  const groupCount = profile.linked_group_count ?? 0;
  const refresh = () => {
    void client.invalidateQueries({ queryKey: queryKeys.profile(profileId) });
    setEditing(false);
  };
  const select = (id: string) => {
    if (id === tab) return;
    transitionTo(() => router.replace(`/profiles/${profileId}?tab=${id}`, { scroll: false }), { readySelector: `[data-tab-panel="${id}"]` });
  };

  return <Page className={styles.page}>
    <ProfileHero profile={profile} hasMessages={!!overview.data?.messages || !person} editing={editing}
      onEdit={() => setEditing((value) => !value)} onPhoto={setPhoto} onLinksChanged={refresh} />
    {editing && detail.data && <FieldEditor profileId={profileId} profile={profile} onSaved={refresh} onCancel={() => setEditing(false)} />}
    {person && <RelationshipPulse profileId={profileId} overview={overview.data} dates={profile.significant_dates ?? []} groupCount={groupCount} />}
    <Tabs className={styles.tabs} label="资料分区" items={tabs} value={tab} onSelect={select} />
    {tab === "overview" && (detail.data ? <ProfileOverview profile={profile} hasPosts={summary.data?.has_posts} /> : detail.isError ? <EmptyState title="资料未能加载" actions={<Button onClick={() => void detail.refetch()}>重试</Button>} /> : <Skeleton height={240} />)}
    {tab === "timeline" && <div data-tab-panel="timeline"><SocialFeed profileId={profileId} /></div>}
    {tab === "photos" && <ProfilePhotoGallery key={profileId} profileId={profileId} name={profile.display_name} />}
    {tab === "messages" && <MessagesTab profileId={profileId} person={person} />}
    {tab === "records" && <div data-tab-panel="records"><RecordsView profileId={profileId} /></div>}
    {tab === "contact-events" && <div data-tab-panel="contact-events"><ContactEvents profileId={profileId} kind={params.get("kind") || "call"} /></div>}
    {tab === "relationships" && <div data-tab-panel="relationships">{person ? <RelationshipsTab profileId={profileId} /> : <GroupOverview profile={profile} />}</div>}
    {tab === "history" && <div data-tab-panel="history"><HistoryView profileId={profileId} /></div>}
    {photo && <PhotoViewer src={photo.src} alt={photo.alt} origin={photo.origin} onClose={() => setPhoto(null)} />}
  </Page>;
}
