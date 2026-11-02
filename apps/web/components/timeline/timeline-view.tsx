"use client";

import { SocialFeed } from "../feed/social-feed";
import { Page, PageHeader } from "../ui/primitives";

export function TimelineView() {
  return <Page width="wide" label="动态">
    <PageHeader title="动态" />
    <SocialFeed sticky />
  </Page>;
}
