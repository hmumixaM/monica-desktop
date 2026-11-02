-- This schema lives independently in each platform's PostgreSQL database.
-- There is deliberately no CRM Profile ID or foreign key in this database.
CREATE TABLE IF NOT EXISTS accounts (
  id UUID PRIMARY KEY,
  external_id TEXT NOT NULL UNIQUE,
  object_kind TEXT NOT NULL CHECK (object_kind IN ('person','group')),
  display_name TEXT NOT NULL,
  username TEXT,
  profile_url TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS account_identifiers (
  identifier TEXT PRIMARY KEY,
  account_id UUID NOT NULL REFERENCES accounts(id)
);
CREATE TABLE IF NOT EXISTS payloads (
  sha256 CHAR(64) PRIMARY KEY,
  value JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Immutable message bodies are stored once, in the CRM's content_objects under
-- the same canonical hash; such payloads keep only the hash and the CRM kind.
-- NOT VALID: rows written before this column existed all carry a value, so skip the full-table scan.
ALTER TABLE payloads ALTER COLUMN value DROP NOT NULL;
ALTER TABLE payloads ADD COLUMN IF NOT EXISTS crm_content_kind TEXT;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'payloads_value_or_reference') THEN
    ALTER TABLE payloads ADD CONSTRAINT payloads_value_or_reference
      CHECK (value IS NOT NULL OR crm_content_kind IS NOT NULL) NOT VALID;
  END IF;
END $$;
CREATE TABLE IF NOT EXISTS objects (
  id UUID PRIMARY KEY,
  account_id UUID NOT NULL REFERENCES accounts(id),
  object_kind TEXT NOT NULL,
  external_id TEXT NOT NULL,
  current_hash CHAR(64) REFERENCES payloads(sha256),
  revision_number BIGINT NOT NULL DEFAULT 0,
  occurred_at TIMESTAMPTZ,
  UNIQUE(account_id,object_kind,external_id)
);
CREATE INDEX IF NOT EXISTS objects_account_kind ON objects(account_id,object_kind,occurred_at DESC,id DESC);
CREATE TABLE IF NOT EXISTS revisions (
  object_id UUID NOT NULL REFERENCES objects(id),
  revision_number BIGINT NOT NULL,
  content_hash CHAR(64) NOT NULL REFERENCES payloads(sha256),
  observed_at TIMESTAMPTZ NOT NULL,
  source_event TEXT NOT NULL,
  PRIMARY KEY(object_id,revision_number),
  UNIQUE(object_id,source_event)
);
CREATE TABLE IF NOT EXISTS observations (
  object_id UUID NOT NULL REFERENCES objects(id),
  source_event TEXT NOT NULL,
  content_hash CHAR(64) NOT NULL REFERENCES payloads(sha256),
  observed_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY(object_id,source_event)
);
CREATE TABLE IF NOT EXISTS media (
  sha256 CHAR(64) PRIMARY KEY,
  media_type TEXT NOT NULL,
  byte_length BIGINT NOT NULL,
  object_path TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS media_links (
  object_id UUID NOT NULL REFERENCES objects(id),
  media_hash CHAR(64) NOT NULL REFERENCES media(sha256),
  role TEXT NOT NULL,
  PRIMARY KEY(object_id,media_hash,role)
);
CREATE TABLE IF NOT EXISTS migration_cursors (
  stream TEXT PRIMARY KEY,
  cursor TEXT,
  object_count BIGINT NOT NULL DEFAULT 0,
  checked_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Immutable normalized inputs. A separate delivery queue feeds the desktop
-- projector independently of WAL history. Payload bytes stay in the
-- content-addressed raw archive referenced by each manifest.
CREATE TABLE IF NOT EXISTS ingest_batches (
  id UUID PRIMARY KEY,
  sequence BIGSERIAL UNIQUE,
  idempotency_key TEXT NOT NULL UNIQUE,
  request_hash CHAR(64) NOT NULL,
  manifest JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS projection_delivery (
  batch_id UUID PRIMARY KEY REFERENCES ingest_batches(id),
  delivered_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS projection_delivery_pending ON projection_delivery(batch_id) WHERE delivered_at IS NULL;
