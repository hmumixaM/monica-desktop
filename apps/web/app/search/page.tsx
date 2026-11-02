import { Suspense } from "react";

import { AppShell } from "../../components/app-shell";
import { SearchView } from "../../components/search/search-view";
import { Loading } from "../../components/ui/primitives";

export default function SearchPage() {
  return <AppShell active="search"><Suspense fallback={<Loading />}><SearchView /></Suspense></AppShell>;
}
