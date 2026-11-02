"use client";

import { Cake, ChevronLeft, Edit3, ExternalLink, Link2, MapPin, MessageCircle, MessagesSquare, MoreHorizontal } from "lucide-react";
import Link from "next/link";
import { type MouseEvent, useState } from "react";

import { localMediaUrl } from "../../lib/media";
import { PROFILE_AVATAR } from "../../lib/transitions";
import { Button, ButtonLink, IconButton } from "../ui/button";
import { Menu } from "../ui/menu";
import { Avatar, SourceMark, providerLabel } from "../ui/primitives";
import { dateMeta, dateText, isSoon } from "./dates";
import { compactIdentities, compactIdentityLabel, profileAddress } from "./fields";
import { GroupSpeechDialog } from "./group-speech";
import styles from "./profile.module.css";
import { SourceAccountsDialog } from "./source-accounts";
import type { Profile } from "./types";

export type Photo = { src: string; alt: string; origin?: HTMLElement | null };
const LINKS = 4;
const CHIPS = 8;

/** The name card: who this is, where to reach them, and the few actions used most often. */
export function ProfileHero({ profile, hasMessages, editing, onEdit, onPhoto, onLinksChanged }: {
  profile: Profile; hasMessages: boolean; editing: boolean;
  onEdit: () => void; onPhoto: (photo: Photo) => void; onLinksChanged: () => void;
}) {
  const [dialog, setDialog] = useState<"accounts" | "speech" | null>(null);
  const person = profile.profile_type === "person";
  const identities = (profile.identities ?? []).filter((identity) => identity.provider !== "monica");
  const chips = compactIdentities(identities);
  const external = chips.filter((identity) => identity.profile_url?.startsWith("http")).slice(0, LINKS);
  const sourceCount = new Set(identities.map((identity) => identity.provider)).size;
  const avatar = localMediaUrl(profile.avatar_media_id);
  const cover = localMediaUrl(profile.cover_media_id);
  const address = person ? profileAddress(profile) : "";
  const birthday = person ? profile.birthday : null;
  const groups = (profile.member_of_groups ?? []).map((group) => ({ profile_id: group.group_profile_id, name: group.display_name }));
  const chatHref = `/profiles/${profile.id}?tab=messages`;
  const show = (src: string, alt: string) => (event: MouseEvent<HTMLElement>) =>
    onPhoto({ src, alt, origin: event.currentTarget.querySelector("img") ?? event.currentTarget });

  return <header className={styles.hero} data-cover={cover ? "" : undefined}>
    <nav className={styles.breadcrumb} aria-label="位置">
      <Link href={person ? "/profiles" : "/profiles?type=group"}><ChevronLeft size={14} aria-hidden="true" />{person ? "联系人" : "群组"}</Link>
    </nav>
    {cover && <button type="button" className={styles.cover} onClick={show(cover, `${profile.display_name}的朋友圈封面`)} aria-label="查看朋友圈封面原图">
      <img src={cover} alt="" />
    </button>}
    <div className={styles.card}>
      <button type="button" className={styles.portrait} disabled={!avatar} aria-label="查看头像原图" data-profile-hero=""
        onClick={avatar ? show(avatar, `${profile.display_name}的头像`) : undefined}>
        <Avatar name={profile.display_name} mediaId={profile.avatar_media_id} size={104} round={person} transitionName={PROFILE_AVATAR} />
      </button>
      <div className={styles.identity}>
        <h1>{profile.display_name}<span className={styles.sourcePill}>{person ? (sourceCount ? `${sourceCount} 个来源` : "待补充来源") : "微信群"}</span></h1>
        {profile.summary && <p className={styles.summary}>{profile.summary}</p>}
        {(address || birthday || external.length > 0) && <p className={styles.meta}>
          {address && <span><MapPin size={14} aria-hidden="true" />{address}</span>}
          {birthday && <span data-soon={isSoon(birthday) ? "" : undefined}><Cake size={14} aria-hidden="true" />{dateText(birthday)}
            {dateMeta(birthday) && <em>{dateMeta(birthday)}</em>}</span>}
          {external.map((identity) => <a key={identity.id} href={identity.profile_url!} target="_blank" rel="noreferrer">
            {identity.provider === "linkedin" && profile.linkedin_avatar_media_id
              ? <img className={styles.linkAvatar} src={localMediaUrl(profile.linkedin_avatar_media_id)} alt="" width={16} height={16} />
              : <SourceMark provider={identity.provider} size={10} decorative />}
            {providerLabel(identity.provider)}<ExternalLink size={11} aria-hidden="true" />
          </a>)}
        </p>}
        {(chips.length > 0 || !!profile.platform_avatars?.length) && <div className={styles.chips}>
          {chips.slice(0, CHIPS).map((identity) => <span key={identity.id} title={identity.external_id} data-provider={identity.provider}>
            <b>{providerLabel(identity.provider)}</b>{compactIdentityLabel(identity)}
          </span>)}
          {chips.length > CHIPS && <span>+{chips.length - CHIPS}</span>}
          {profile.platform_avatars?.map((version) => <button type="button" key={`${version.provider}:${version.media_id}`} className={styles.versionAvatar}
            aria-label={`查看${providerLabel(version.provider)}头像`}
            onClick={show(`/api/v1/media/${version.media_id}`, `${profile.display_name}的${providerLabel(version.provider)}头像`)}>
            <img src={localMediaUrl(version.media_id)} alt="" width={28} height={28} loading="lazy" />
            <SourceMark provider={version.provider} size={8} />
          </button>)}
        </div>}
      </div>
      <div className={styles.actions}>
        <div className={styles.desktopActions}>
          {hasMessages && <ButtonLink href={chatHref}><MessageCircle size={15} />聊天</ButtonLink>}
          {person && <Button onClick={() => setDialog("speech")}><MessagesSquare size={15} />群聊发言</Button>}
          <Button onClick={() => setDialog("accounts")}><Link2 size={15} />来源账号</Button>
          <Button variant="primary" onClick={onEdit} aria-expanded={editing}><Edit3 size={15} />{editing ? "关闭编辑" : "编辑"}</Button>
        </div>
        <div className={styles.mobileActions}>
          {hasMessages && <ButtonLink href={chatHref} size="sm"><MessageCircle size={15} />聊天</ButtonLink>}
          <Menu trigger={<IconButton label="更多操作" outlined><MoreHorizontal size={18} /></IconButton>} items={[
            { label: editing ? "关闭编辑" : "编辑资料", icon: <Edit3 size={15} />, onSelect: onEdit },
            ...(person ? [{ label: "群聊发言", icon: <MessagesSquare size={15} />, onSelect: () => setDialog("speech") }] : []),
            { label: "管理来源账号", icon: <Link2 size={15} />, onSelect: () => setDialog("accounts") },
          ]} />
        </div>
      </div>
    </div>
    {dialog === "accounts" && <SourceAccountsDialog profileId={profile.id} profileType={profile.profile_type} onChanged={onLinksChanged} onClose={() => setDialog(null)} />}
    {dialog === "speech" && <GroupSpeechDialog profileId={profile.id} groups={groups} onClose={() => setDialog(null)} />}
  </header>;
}
