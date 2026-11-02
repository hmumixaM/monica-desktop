-- Durable WAL inbox. Capture commits here before acknowledging the source slot.
ALTER TABLE jobs ADD COLUMN enqueued_at TIMESTAMPTZ;
ALTER TABLE jobs ALTER COLUMN enqueued_at SET DEFAULT clock_timestamp();
CREATE INDEX pending_import_receipts ON import_batches(received_at) WHERE status='queued';
CREATE TABLE cdc_events (
  id BIGSERIAL PRIMARY KEY,
  provider TEXT NOT NULL CHECK (provider IN ('wechat','instagram','linkedin','crm')),
  commit_lsn PG_LSN NOT NULL,
  ordinal INTEGER NOT NULL,
  relation_name TEXT NOT NULL,
  operation TEXT NOT NULL,
  before_row JSONB,
  after_row JSONB,
  committed_at TIMESTAMPTZ NOT NULL,
  captured_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(provider,commit_lsn,ordinal)
);
CREATE INDEX cdc_provider_history ON cdc_events(provider,id DESC);
CREATE TABLE projection_inbox (
  id BIGSERIAL PRIMARY KEY,
  provider TEXT NOT NULL,
  source_batch_id UUID NOT NULL,
  manifest JSONB NOT NULL,
  subject_keys TEXT[] NOT NULL,
  received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  projected_at TIMESTAMPTZ,
  UNIQUE(provider,source_batch_id)
);
CREATE INDEX projection_pending ON projection_inbox(provider,id) WHERE projected_at IS NULL;
CREATE TABLE cdc_consumers (
  provider TEXT PRIMARY KEY,
  slot_name TEXT NOT NULL,
  established BOOLEAN NOT NULL DEFAULT false,
  captured_lsn PG_LSN,
  heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  retained_wal_bytes BIGINT NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'starting',
  error_code TEXT
);
CREATE TABLE media_dependencies (
  id TEXT PRIMARY KEY,
  cache_name TEXT NOT NULL,
  record JSONB NOT NULL,
  state TEXT NOT NULL DEFAULT 'waiting',
  reason TEXT NOT NULL DEFAULT 'source_pending',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX media_dependencies_due ON media_dependencies(next_attempt_at);
CREATE OR REPLACE FUNCTION job_subject(kind text, body jsonb) RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE
    WHEN kind='source.project' THEN body->>'event_id'
    WHEN left(kind,3)='ai.' THEN coalesce(body->>'profile_id',body->>'post_id',body->>'story_id',body->>'period','all')
    WHEN kind='media.dimensions' THEN body->>'media_id'
    WHEN kind='media.download' THEN encode(sha256(convert_to(jsonb_build_array(body->>'entity_type',body->>'entity_id',body->>'role',body->>'url')::text,'UTF8')),'hex')
    WHEN kind='metrics.refresh' THEN body->>'batch_id'
  END
$$;
CREATE OR REPLACE FUNCTION job_resource(kind text) RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE WHEN kind='source.project' THEN 'projection'
    WHEN left(kind,3)='ai.' THEN 'ai'
    WHEN kind='media.download' THEN 'media' ELSE 'db' END
$$;
