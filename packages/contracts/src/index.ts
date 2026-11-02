export type ProfileType = "person" | "group";
export type Provider = "wechat" | "instagram" | "linkedin" | "monica";

export interface Profile {
  id: string;
  profile_type: ProfileType;
  display_name: string;
  summary?: string | null;
  created_at: string;
  archived_at?: string | null;
}

export interface FieldRevision {
  id: string;
  profile_id: string;
  field_key: string;
  content_hash: string;
  source_type: string;
  operation: "import" | "manual_edit" | "restore" | "accept_import";
  observed_at: string;
  is_conflict: boolean;
  value?: unknown;
}

export interface ImportBatchRecord {
  external_id: string;
  content: unknown;
  profile_id?: string;
  entity_type?: string;
  provider?: Provider;
  occurred_at?: string;
  source_updated_at?: string;
  media?: Array<Record<string, unknown>>;
}

export interface ImportBatchRequest {
  source: Provider;
  stream: string;
  schema_version: number;
  batch_id: string;
  idempotency_key: string;
  cursor_before?: string | null;
  cursor_after?: string | null;
  observed_at: string;
  records: ImportBatchRecord[];
  raw_objects: Array<Record<string, unknown>>;
  media_manifest: Array<Record<string, unknown>>;
}

export interface TimelineItem {
  id: string;
  provider: string;
  external_id: string;
  occurred_at?: string | null;
  title: string;
  summary?: string | null;
  content_hash: string;
}
