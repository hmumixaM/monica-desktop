CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS app_users (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  totp_secret TEXT,
  passkey_enabled BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_login_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS sessions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
  token_hash TEXT NOT NULL UNIQUE,
  expires_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  revoked_at TIMESTAMPTZ,
  user_agent TEXT,
  ip_address INET
);

CREATE TABLE IF NOT EXISTS audit_events (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID REFERENCES app_users(id) ON DELETE SET NULL,
  action TEXT NOT NULL,
  entity_type TEXT,
  entity_id UUID,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS profiles (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  profile_type TEXT NOT NULL CHECK (profile_type IN ('person', 'group')),
  display_name TEXT NOT NULL,
  summary TEXT,
  avatar_media_id UUID,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  archived_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS profiles_display_name_idx ON profiles USING gin (to_tsvector('simple', display_name));

CREATE TABLE IF NOT EXISTS content_objects (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  sha256 CHAR(64) NOT NULL,
  content_kind TEXT NOT NULL,
  encoding TEXT NOT NULL DEFAULT 'utf-8',
  byte_length BIGINT NOT NULL DEFAULT 0,
  storage_uri TEXT,
  payload_json JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (content_kind, sha256)
);

CREATE TABLE IF NOT EXISTS media_assets (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  content_object_id UUID NOT NULL REFERENCES content_objects(id),
  sha256 CHAR(64) NOT NULL UNIQUE,
  media_type TEXT NOT NULL,
  byte_length BIGINT NOT NULL,
  object_path TEXT NOT NULL,
  width INTEGER,
  height INTEGER,
  duration_ms BIGINT,
  capture_at TIMESTAMPTZ,
  source_type TEXT,
  source_record_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'profiles_avatar_media_fk'
  ) THEN
    ALTER TABLE profiles ADD CONSTRAINT profiles_avatar_media_fk
      FOREIGN KEY (avatar_media_id) REFERENCES media_assets(id) DEFERRABLE INITIALLY DEFERRED;
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS import_batches (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source TEXT NOT NULL,
  stream TEXT NOT NULL,
  schema_version INTEGER NOT NULL,
  batch_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  cursor_before TEXT,
  cursor_after TEXT,
  raw_manifest_object_id UUID REFERENCES content_objects(id),
  status TEXT NOT NULL DEFAULT 'received',
  received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at TIMESTAMPTZ,
  inserted_count INTEGER NOT NULL DEFAULT 0,
  changed_count INTEGER NOT NULL DEFAULT 0,
  unchanged_count INTEGER NOT NULL DEFAULT 0,
  error_summary TEXT,
  UNIQUE (source, stream, batch_id)
);

CREATE TABLE IF NOT EXISTS source_observations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source TEXT NOT NULL,
  stream TEXT NOT NULL,
  external_id TEXT NOT NULL,
  batch_id UUID NOT NULL REFERENCES import_batches(id) ON DELETE CASCADE,
  source_cursor TEXT,
  content_hash CHAR(64) NOT NULL,
  observed_at TIMESTAMPTZ NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('new', 'changed', 'unchanged', 'missing', 'withdrawn')),
  source_updated_at TIMESTAMPTZ,
  UNIQUE (batch_id, source, stream, external_id)
);
CREATE INDEX IF NOT EXISTS source_observations_lookup_idx ON source_observations(source, stream, external_id, observed_at DESC);

CREATE TABLE IF NOT EXISTS profile_field_revisions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  profile_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  field_key TEXT NOT NULL,
  content_object_id UUID NOT NULL REFERENCES content_objects(id),
  content_hash CHAR(64) NOT NULL,
  source_type TEXT NOT NULL,
  source_record_id TEXT,
  actor_user_id UUID REFERENCES app_users(id) ON DELETE SET NULL,
  observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  effective_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  imported_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  previous_revision_id UUID REFERENCES profile_field_revisions(id),
  operation TEXT NOT NULL CHECK (operation IN ('import', 'manual_edit', 'restore', 'accept_import')),
  is_conflict BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS profile_field_revisions_history_idx ON profile_field_revisions(profile_id, field_key, effective_at DESC);

CREATE TABLE IF NOT EXISTS profile_field_current (
  profile_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  field_key TEXT NOT NULL,
  current_revision_id UUID NOT NULL REFERENCES profile_field_revisions(id),
  current_content_hash CHAR(64) NOT NULL,
  current_source_type TEXT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (profile_id, field_key)
);

CREATE TABLE IF NOT EXISTS identities (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  profile_id UUID REFERENCES profiles(id) ON DELETE SET NULL,
  provider TEXT NOT NULL,
  external_id TEXT NOT NULL,
  username TEXT,
  profile_url TEXT,
  display_name TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (provider, external_id)
);

CREATE TABLE IF NOT EXISTS group_memberships (
  group_profile_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  person_profile_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  role TEXT,
  joined_at TIMESTAMPTZ,
  left_at TIMESTAMPTZ,
  observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  source_record_id TEXT,
  PRIMARY KEY (group_profile_id, person_profile_id)
);

CREATE TABLE IF NOT EXISTS conversations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_type TEXT NOT NULL CHECK (conversation_type IN ('direct', 'group')),
  profile_id UUID REFERENCES profiles(id) ON DELETE CASCADE,
  external_id TEXT,
  UNIQUE (conversation_type, external_id)
);

CREATE TABLE IF NOT EXISTS message_events (
  id UUID NOT NULL DEFAULT gen_random_uuid(),
  conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  sender_profile_id UUID REFERENCES profiles(id) ON DELETE SET NULL,
  occurred_at TIMESTAMPTZ NOT NULL,
  message_type TEXT NOT NULL,
  content_object_id UUID REFERENCES content_objects(id),
  raw_object_id UUID REFERENCES content_objects(id),
  source_record_id TEXT NOT NULL,
  PRIMARY KEY (id, occurred_at),
  UNIQUE (conversation_id, source_record_id, occurred_at)
) PARTITION BY RANGE (occurred_at);
CREATE TABLE IF NOT EXISTS message_events_default PARTITION OF message_events DEFAULT;
CREATE INDEX IF NOT EXISTS message_events_conversation_idx ON message_events(conversation_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS message_events_sender_idx ON message_events(sender_profile_id, occurred_at DESC);

CREATE TABLE IF NOT EXISTS social_posts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  profile_id UUID REFERENCES profiles(id) ON DELETE SET NULL,
  provider TEXT NOT NULL,
  external_id TEXT NOT NULL,
  content_object_id UUID REFERENCES content_objects(id),
  occurred_at TIMESTAMPTZ,
  source_updated_at TIMESTAMPTZ,
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  current_content_hash CHAR(64) NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  UNIQUE(provider, external_id)
);

CREATE TABLE IF NOT EXISTS social_post_revisions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  post_id UUID NOT NULL REFERENCES social_posts(id) ON DELETE CASCADE,
  content_object_id UUID NOT NULL REFERENCES content_objects(id),
  content_hash CHAR(64) NOT NULL,
  observed_at TIMESTAMPTZ NOT NULL,
  source_record_id TEXT,
  UNIQUE(post_id, content_hash)
);

CREATE TABLE IF NOT EXISTS social_stories (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  profile_id UUID REFERENCES profiles(id) ON DELETE SET NULL,
  provider TEXT NOT NULL,
  external_id TEXT NOT NULL,
  content_object_id UUID REFERENCES content_objects(id),
  occurred_at TIMESTAMPTZ,
  expires_at TIMESTAMPTZ,
  current_content_hash CHAR(64) NOT NULL,
  coverage_status TEXT NOT NULL DEFAULT 'partial',
  UNIQUE(provider, external_id)
);

CREATE TABLE IF NOT EXISTS social_story_revisions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  story_id UUID NOT NULL REFERENCES social_stories(id) ON DELETE CASCADE,
  content_object_id UUID NOT NULL REFERENCES content_objects(id),
  content_hash CHAR(64) NOT NULL,
  observed_at TIMESTAMPTZ NOT NULL,
  UNIQUE(story_id, content_hash)
);

CREATE TABLE IF NOT EXISTS media_links (
  media_id UUID NOT NULL REFERENCES media_assets(id) ON DELETE CASCADE,
  entity_type TEXT NOT NULL,
  entity_id UUID NOT NULL,
  role TEXT NOT NULL DEFAULT 'primary',
  PRIMARY KEY(media_id, entity_type, entity_id, role)
);

CREATE TABLE IF NOT EXISTS activities (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  activity_type TEXT NOT NULL,
  occurred_at TIMESTAMPTZ NOT NULL,
  title TEXT NOT NULL,
  body TEXT,
  source_type TEXT NOT NULL DEFAULT 'manual',
  source_record_id TEXT,
  created_by UUID REFERENCES app_users(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS activity_participants (
  activity_id UUID NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
  profile_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  PRIMARY KEY(activity_id, profile_id)
);

CREATE TABLE IF NOT EXISTS relationship_types (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name TEXT NOT NULL UNIQUE,
  is_system BOOLEAN NOT NULL DEFAULT FALSE
);
INSERT INTO relationship_types(name, is_system) VALUES
  ('高中同学', TRUE), ('同事', TRUE), ('邻居', TRUE), ('家人', TRUE), ('客户', TRUE)
ON CONFLICT (name) DO NOTHING;

CREATE TABLE IF NOT EXISTS relationship_edges (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  from_profile_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  to_profile_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  relationship_type_id UUID NOT NULL REFERENCES relationship_types(id),
  note TEXT,
  confidence NUMERIC(5,4),
  valid_from TIMESTAMPTZ,
  valid_to TIMESTAMPTZ,
  source_type TEXT NOT NULL DEFAULT 'manual',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(from_profile_id, to_profile_id, relationship_type_id)
);

CREATE TABLE IF NOT EXISTS tags (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name TEXT NOT NULL UNIQUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS tag_memberships (
  tag_id UUID NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  profile_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  source_type TEXT NOT NULL DEFAULT 'manual',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY(tag_id, profile_id)
);

CREATE TABLE IF NOT EXISTS profile_metrics_current (
  profile_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  metric_key TEXT NOT NULL,
  value_json JSONB NOT NULL,
  source_watermark TEXT,
  computed_at TIMESTAMPTZ,
  calculation_version TEXT NOT NULL DEFAULT '1',
  freshness_state TEXT NOT NULL DEFAULT 'fresh',
  PRIMARY KEY(profile_id, metric_key)
);
CREATE TABLE IF NOT EXISTS global_metric_snapshots (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  metric_key TEXT NOT NULL,
  value_json JSONB NOT NULL,
  source_watermark TEXT,
  computed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  calculation_version TEXT NOT NULL DEFAULT '1',
  freshness_state TEXT NOT NULL DEFAULT 'fresh'
);

CREATE TABLE IF NOT EXISTS jobs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  job_type TEXT NOT NULL,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  status TEXT NOT NULL DEFAULT 'queued',
  attempts INTEGER NOT NULL DEFAULT 0,
  available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  locked_at TIMESTAMPTZ,
  locked_by TEXT,
  completed_at TIMESTAMPTZ,
  error TEXT
);
CREATE INDEX IF NOT EXISTS jobs_claim_idx ON jobs(status, available_at, id);
