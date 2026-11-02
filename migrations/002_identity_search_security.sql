CREATE TABLE IF NOT EXISTS webauthn_credentials (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
  credential_id TEXT NOT NULL UNIQUE,
  public_key BYTEA NOT NULL,
  sign_count BIGINT NOT NULL DEFAULT 0,
  label TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_used_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS webauthn_challenges (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
  challenge TEXT NOT NULL UNIQUE,
  purpose TEXT NOT NULL CHECK (purpose IN ('registration', 'authentication')),
  expires_at TIMESTAMPTZ NOT NULL,
  consumed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS identity_candidates (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  identity_id UUID NOT NULL REFERENCES identities(id) ON DELETE CASCADE,
  candidate_profile_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  score NUMERIC(6,5) NOT NULL,
  evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'accepted', 'rejected')),
  reviewed_by UUID REFERENCES app_users(id) ON DELETE SET NULL,
  reviewed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS identity_candidates_queue_idx ON identity_candidates(status, score DESC);

CREATE TABLE IF NOT EXISTS relationship_edge_revisions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  edge_id UUID NOT NULL REFERENCES relationship_edges(id) ON DELETE CASCADE,
  operation TEXT NOT NULL CHECK (operation IN ('created', 'edited', 'removed', 'restored')),
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  source_type TEXT NOT NULL DEFAULT 'manual',
  actor_user_id UUID REFERENCES app_users(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tag_membership_revisions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tag_id UUID NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  profile_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  operation TEXT NOT NULL CHECK (operation IN ('added', 'removed', 'restored')),
  source_type TEXT NOT NULL DEFAULT 'manual',
  actor_user_id UUID REFERENCES app_users(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS search_documents (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  entity_type TEXT NOT NULL,
  entity_id UUID NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  search_vector TSVECTOR GENERATED ALWAYS AS (
    to_tsvector('simple', coalesce(title, '') || ' ' || coalesce(summary, ''))
  ) STORED,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(entity_type, entity_id)
);
CREATE INDEX IF NOT EXISTS search_documents_vector_idx ON search_documents USING gin(search_vector);

CREATE TABLE IF NOT EXISTS social_comments (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  provider TEXT NOT NULL,
  external_id TEXT NOT NULL,
  post_id UUID REFERENCES social_posts(id) ON DELETE CASCADE,
  author_profile_id UUID REFERENCES profiles(id) ON DELETE SET NULL,
  content_object_id UUID REFERENCES content_objects(id),
  occurred_at TIMESTAMPTZ,
  current_content_hash CHAR(64) NOT NULL,
  UNIQUE(provider, external_id)
);
