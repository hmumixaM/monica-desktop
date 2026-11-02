CREATE TABLE IF NOT EXISTS source_cursors (
  source TEXT NOT NULL,
  stream TEXT NOT NULL,
  cursor_value TEXT,
  batch_id UUID NOT NULL REFERENCES import_batches(id) ON DELETE RESTRICT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (source, stream)
);
