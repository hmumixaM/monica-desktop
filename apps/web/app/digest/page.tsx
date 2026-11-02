import { Suspense } from "react";

import { AppShell } from "../../components/app-shell";
import { DigestView } from "../../components/digest/digest-view";

export default function DigestPage() {
  return <AppShell active="digest"><Suspense><DigestView /></Suspense></AppShell>;
}
