import { AppShell } from "../../../components/app-shell";
import { ProfileView } from "../../../components/profile/profile-page";

export default async function ProfilePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <AppShell active="profiles"><ProfileView profileId={id} /></AppShell>;
}
