import type { SignificantDate } from "../../lib/api-types";

export type Field = { field_key: string; value: unknown; current_source_type: string; updated_at: string; current_content_hash?: string | null };
export type Identity = {
  id: string; provider: string; external_id: string; username?: string | null; profile_url?: string | null; display_name?: string | null;
  status?: string | null; first_seen_at?: string | null; last_seen_at?: string | null;
};
export type StructuredFact = {
  fact_type: "address" | "employment" | "education"; value: unknown; current_source_type: string; updated_at: string;
  company_name?: string | null; school_name?: string | null;
};
export type GroupLink = {
  group_profile_id: string; person_profile_id: string; display_name: string; profile_type: "person" | "group"; avatar_media_id?: string | null;
  role?: string | null; joined_at?: string | null; left_at?: string | null; observed_at?: string | null; source_record_id?: string | null;
};
type Presentation = { instagram_accounts?: Record<string, unknown>[]; sources: Record<string, Record<string, unknown>>; facts: StructuredFact[] };

export type Profile = {
  id: string; profile_type: "person" | "group"; display_name: string; summary?: string | null;
  avatar_media_id?: string | null; cover_media_id?: string | null; linkedin_cover_media_id?: string | null; linkedin_avatar_media_id?: string | null;
  platform_avatars?: { provider: string; media_id: string; source_account_id?: string }[];
  tags?: { id: string; name: string; source_type: string }[];
  group_summary?: { messages: number; active_senders: number; first?: string; last?: string };
  presentation?: Presentation; fields?: Field[]; identities?: Identity[]; structured_facts?: StructuredFact[];
  group_members?: GroupLink[]; group_owners?: GroupLink[]; member_of_groups?: GroupLink[]; group_member_count?: number; linked_group_count?: number;
  birthday?: SignificantDate | null; significant_dates?: SignificantDate[];
};

export type Revision = { id: string; field_key: string; value: unknown; source_type: string; operation: string; observed_at: string; is_conflict: boolean };

export type Relationship = {
  id: string; related_profile_id: string; related_display_name: string; related_profile_type: "person" | "group"; relationship_type: string;
  direction: string; source_type: string; note?: string | null; confidence?: number | null; valid_from?: string | null; valid_to?: string | null;
};
