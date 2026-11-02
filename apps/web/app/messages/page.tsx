import { Suspense } from "react";

import { AppShell } from "../../components/app-shell";
import { MessagesView } from "../../components/messages/messages-view";

export default function MessagesPage() {
  return <AppShell active="messages"><Suspense><MessagesView /></Suspense></AppShell>;
}
