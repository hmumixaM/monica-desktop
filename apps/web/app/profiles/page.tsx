import { Suspense } from "react";

import { AppShell } from "../../components/app-shell";
import { ContactsView } from "../../components/contacts/contacts-view";

export default function ProfilesPage() {
  return <AppShell active="profiles"><Suspense><ContactsView /></Suspense></AppShell>;
}
