import { Suspense } from "react";

import { AppShell } from "../../components/app-shell";
import { RelationshipsView } from "../../components/relationships/relationships-view";

export default function RelationshipsPage() {
  return <AppShell active="relationships"><Suspense><RelationshipsView /></Suspense></AppShell>;
}
