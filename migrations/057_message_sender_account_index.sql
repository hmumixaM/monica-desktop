-- A person's group speech is counted by the WeChat accounts linked to them. Without this index the
-- per-profile social metrics scanned every message and hit the 15 s statement timeout.
-- Migrations share one transaction, so on a large existing database build the index per partition
-- with CREATE INDEX CONCURRENTLY and attach it under this name first; this statement is then a no-op.
CREATE INDEX IF NOT EXISTS message_events_sender_account_idx
  ON message_events(sender_account_id, conversation_id)
  WHERE sender_account_id IS NOT NULL;
