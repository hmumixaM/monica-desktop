import { Suspense } from "react";

import { AppShell } from "../../components/app-shell";
import { InsightsView } from "../../components/insights/insights-view";

export default function InsightsPage() {
  return <AppShell active="insights"><Suspense><InsightsView /></Suspense></AppShell>;
}
