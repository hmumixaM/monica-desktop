// Response shapes of the cached aggregate endpoints (see docs/API_UI_CONTRACT.md).
// Optional fields are omitted by the API when a block has no data.

export type Provider = "wechat" | "instagram" | "linkedin" | "monica";

export interface MonthCount { month: string; sent: number; received: number }

export interface PersonRef {
  profile_id: string;
  name: string;
  avatar_media_id?: string;
}

export type DateKind = "birthday" | "anniversary" | "other";
export type DatePrecision = "month_day" | "approximate" | "exact";

/** A birthday, anniversary or other date; `years` is the age or anniversary count, `next` the next yearly occurrence. */
export interface SignificantDate {
  id: string;
  kind: DateKind;
  label: string | null;
  precision: DatePrecision;
  year: number | null;
  month: number | null;
  day: number | null;
  years: number | null;
  years_exact: boolean;
  next: string | null;
  days_until: number | null;
}

export interface OverviewHeader {
  id: string;
  type: "person" | "group";
  name: string;
  summary?: string;
  note?: string;
  company?: string;
  title?: string;
  headline?: string;
  city?: string;
  birthday?: SignificantDate;
  avatar_media_id?: string;
  avatars?: { provider: string; media_id: string }[];
  sources?: Provider[];
}

export interface CallSummary {
  total: number; voice: number; video: number; completed: number; incoming: number; outgoing: number;
  duration_seconds: number; known_duration: number; inferred_type: number;
}

export interface MoneySummary {
  total: number; transfer: number; red_packet: number; unknown_amount: number; unknown_direction: number;
  currencies: Record<string, { in: number; out: number }>;
}

export interface ProfileOverview {
  header: OverviewHeader;
  messages?: {
    months: MonthCount[]; total: number; sent: number; received: number;
    first_contact_at: string | null; last_contact_at: string; last_group_at: string | null; group_total: number;
  };
  contact_events?: { calls?: CallSummary; money?: MoneySummary };
  relationships?: (PersonRef & { relationship_type: string; direction: "outgoing" | "incoming" | "both" })[];
  groups?: (PersonRef & { role?: string; message_count?: number; last_message_at?: string })[];
  tags?: { id: string; name: string }[];
  linkedin_experience?: {
    type: "employment" | "education"; title?: string; company?: string; school?: string; degree?: string;
    field_of_study?: string; location?: string; start_date?: string; end_date?: string; is_current?: boolean;
    description?: string; company_id?: string; school_id?: string;
  }[];
  post_stats?: Partial<Record<Provider, { posts: number; last_posted_at: string | null }>>;
  places?: { label: string; latitude: number; longitude: number; count: number }[];
  recent_posts?: {
    id: string; provider: Provider; occurred_at?: string; text?: string;
    thumbnail_media_id?: string; width?: number; height?: number; media_count?: number;
  }[];
}

export interface HomePerson extends PersonRef {
  last_contact_at?: string;
  last_group_at?: string;
  direct_message_count?: number;
  group_message_count?: number;
  months?: MonthCount[];
  calls?: number;
}

export interface HomeSummary {
  key_numbers: { contacts: number; groups: number; messages_30d: number; active_contacts_30d: number };
}

export interface OrganizationCount { id: string; name: string; people: number }

export interface InsightsSummary {
  dormant_days: 90 | 180 | 365;
  dormant_contacts?: HomePerson[];
  upcoming_dates?: (PersonRef & { kind: DateKind; label?: string; date: string; days_until?: number; years?: number })[];
  recent_dates?: (PersonRef & { kind: DateKind; label?: string; date: string; days_ago: number })[];
  recent_posters?: (PersonRef & { posts?: number; stories?: number; latest_at: string; providers: string[]; thumbnail_media_ids?: string[] })[];
  recent_contacts?: HomePerson[];
  top_companies?: OrganizationCount[];
  top_schools?: OrganizationCount[];
}

export interface TimelineMedia {
  id?: string;
  media_type?: string;
  width?: number | null;
  height?: number | null;
  source_url?: string;
  thumbnail_id?: string;
  ready: boolean;
  [key: string]: unknown;
}

export interface TimelineItem {
  id: string;
  provider: string;
  external_id: string;
  occurred_at: string | null;
  title: string;
  profile_id: string | null;
  profile_name: string | null;
  avatar_media_id: string | null;
  summary: string | null;
  media_count: number;
  like_count: number;
  comment_count: number;
  location: unknown;
  shared_link: unknown;
  media: TimelineMedia[];
  event_metadata: unknown;
  cursor: string;
}

/** One /api/v1/search row; the fields after `summary` are absent on older API builds. */
export interface SearchHit {
  id: string;
  entity_type: "profile" | "tag" | "timeline" | "activity";
  title: string;
  summary: string;
  occurred_at?: string | null;
  profile_id?: string | null;
  profile_name?: string | null;
  kind?: string | null;
  avatar_media_id?: string | null;
  provider?: string | null;
}

export interface ConversationItem {
  id: string;
  conversation_type: "direct" | "group";
  external_id: string;
  profile_id: string | null;
  display_name: string | null;
  avatar_media_id: string | null;
  last_message_at: string | null;
  last_message_preview: string | null;
  message_count: number;
  cursor: string;
}

export interface RawDirectory {
  columns: string[];
  profiles: unknown[][];
  tags: [string, string][];
  companies: [string, string][];
  schools: [string, string][];
}

export interface RawPostIndex {
  columns: string[];
  posts: [string, string | null, string, string | null, string][];
}
