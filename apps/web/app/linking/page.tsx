import {Suspense} from "react";
import {AppShell} from "../../components/app-shell";
import {LinkingView} from "../../components/linking/linking-view";

export default function LinkingPage() {
  return <AppShell active="linking"><Suspense><LinkingView /></Suspense></AppShell>;
}
