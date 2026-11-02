CREATE TABLE IF NOT EXISTS media_uploads (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  expected_sha256 CHAR(64),
  media_type TEXT NOT NULL,
  expected_byte_length BIGINT,
  temp_path TEXT NOT NULL,
  next_chunk INTEGER NOT NULL DEFAULT 0,
  received_bytes BIGINT NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'uploading' CHECK (status IN ('uploading', 'complete', 'failed')),
  created_by UUID REFERENCES app_users(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at TIMESTAMPTZ
);
