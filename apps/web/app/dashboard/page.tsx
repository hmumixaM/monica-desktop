import { Suspense } from "react";

import { AppShell } from "../../components/app-shell";
import { HomeView } from "../../components/home/home-view";
import { LegacyTimelineRedirect } from "../../components/home/legacy-redirect";

export default function DashboardPage() {
  return <AppShell active="dashboard"><LegacyTimelineRedirect /><Suspense><HomeView /></Suspense></AppShell>;
}
