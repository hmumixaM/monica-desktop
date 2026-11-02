import { SuggestionsView } from "../../components/ai/suggestions-view";
import { AppShell } from "../../components/app-shell";

export default function SuggestionsPage() {
  return <AppShell active="suggestions"><SuggestionsView /></AppShell>;
}
