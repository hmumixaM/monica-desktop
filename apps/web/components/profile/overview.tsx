"use client";

import { Deferred } from "../ui/deferred";
import { ProfileAi } from "../ai/profile-ai";
import { ProfilePhotos } from "../media-attachments";
import { SocialFeed } from "../feed/social-feed";
import { SocialAnalytics } from "../social/social-analytics";
import { PlatformBios, WeChatDetails } from "./about";
import { InstagramDossier, LinkedInDossier } from "./career";
import { ContactEventSidebar } from "./contact-events";
import { SignificantDates } from "./dates";
import { GroupOverview, GroupsBlock, OwnersBlock, RelationshipsBlock } from "./people";
import { ManualDetails } from "./manual-details";
import { LocationSummary } from "./places";
import { ProfileTags } from "./profile-tags";
import styles from "./profile.module.css";
import type { Profile } from "./types";

/**
 * Facts on the left, what is happening on the right. The page scrolls as one; on a phone the facts come first.
 * Every block renders nothing when the API has nothing for it.
 */
export function ProfileOverview({ profile, hasPosts = false }: { profile: Profile; hasPosts?: boolean }) {
  const person = profile.profile_type === "person";
  const tags = <ProfileTags profileId={profile.id} tags={profile.tags ?? []} />;
  if (!person) return <div className={styles.overview} data-tab-panel="overview">
    <div className={styles.factsColumn}>
      <PlatformBios profile={profile} />
      <WeChatDetails profile={profile} />
      {tags}
      <OwnersBlock profile={profile} />
    </div>
    <div className={styles.activityColumn}><GroupOverview profile={profile} /></div>
  </div>;
  return <div className={styles.overview} data-tab-panel="overview">
    <div className={styles.factsColumn}>
      <PlatformBios profile={profile} />
      <SignificantDates profileId={profile.id} dates={profile.significant_dates ?? []} />
      {tags}
      <ManualDetails profile={profile} />
      <Deferred><ProfilePhotos profileId={profile.id} /></Deferred>
      <WeChatDetails profile={profile} />
      <Deferred><ContactEventSidebar profileId={profile.id} /></Deferred>
      <Deferred><RelationshipsBlock profileId={profile.id} /></Deferred>
      <GroupsBlock profile={profile} />
    </div>
    <div className={styles.activityColumn}>
      <Deferred><ProfileAi profileId={profile.id} /></Deferred>
      {hasPosts && <div className={styles.feedPanel}><SocialFeed profileId={profile.id} preview={4} title="近况" /></div>}
      <Deferred><SocialAnalytics profileId={profile.id} /></Deferred>
      <Deferred><LocationSummary profileId={profile.id} /></Deferred>
      <Deferred><LinkedInDossier profile={profile} /></Deferred>
      <Deferred><InstagramDossier profile={profile} /></Deferred>
    </div>
  </div>;
}
