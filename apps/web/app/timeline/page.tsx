import { Suspense } from "react";

import { AppShell } from "../../components/app-shell";
import { TimelineView } from "../../components/timeline/timeline-view";

export default function TimelinePage() {
  return <AppShell active="timeline"><Suspense><TimelineView /></Suspense></AppShell>;
}
