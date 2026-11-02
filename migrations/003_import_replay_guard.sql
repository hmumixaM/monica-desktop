CREATE TABLE IF NOT EXISTS import_request_nonces (
  nonce TEXT PRIMARY KEY,
  received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS import_request_nonces_expiry_idx
  ON import_request_nonces(expires_at);
