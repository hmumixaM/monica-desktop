-- Incrementally maintained message statistics. Inserting messages marks their
-- conversations dirty; contact_stats.refresh recomputes only those. Per-person
-- views aggregate conversations through their current link, so relinking an
-- account moves its history without rewriting statistics.
CREATE TABLE IF NOT EXISTS conversation_message_months (
  conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  month DATE NOT NULL,
  received BIGINT NOT NULL,
  sent BIGINT NOT NULL,
  first_at TIMESTAMPTZ NOT NULL,
  last_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (conversation_id, month)
);

CREATE TABLE IF NOT EXISTS conversation_stats_dirty (
  conversation_id UUID PRIMARY KEY
);

ALTER TABLE conversations
  ADD COLUMN IF NOT EXISTS last_message_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS last_message_preview TEXT,
  ADD COLUMN IF NOT EXISTS message_count BIGINT NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS conversations_recent ON conversations(last_message_at DESC NULLS LAST, id DESC);

CREATE OR REPLACE FUNCTION mark_conversation_stats_dirty() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO conversation_stats_dirty(conversation_id)
  SELECT DISTINCT conversation_id FROM changed_rows
  ON CONFLICT DO NOTHING;
  RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION mark_relinked_conversations_dirty() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO conversation_stats_dirty(conversation_id)
  SELECT n.id FROM new_rows n JOIN old_rows o ON o.id = n.id
  WHERE (n.source_account_id, n.profile_id) IS DISTINCT FROM (o.source_account_id, o.profile_id)
  ON CONFLICT DO NOTHING;
  RETURN NULL;
END $$;

DROP TRIGGER IF EXISTS conversation_stats_insert ON message_events;
CREATE TRIGGER conversation_stats_insert AFTER INSERT ON message_events
  REFERENCING NEW TABLE AS changed_rows FOR EACH STATEMENT EXECUTE FUNCTION mark_conversation_stats_dirty();
DROP TRIGGER IF EXISTS conversation_stats_delete ON message_events;
CREATE TRIGGER conversation_stats_delete AFTER DELETE ON message_events
  REFERENCING OLD TABLE AS changed_rows FOR EACH STATEMENT EXECUTE FUNCTION mark_conversation_stats_dirty();
DROP TRIGGER IF EXISTS conversation_stats_relink ON conversations;
CREATE TRIGGER conversation_stats_relink AFTER UPDATE ON conversations
  REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
  FOR EACH STATEMENT EXECUTE FUNCTION mark_relinked_conversations_dirty();

INSERT INTO conversation_stats_dirty(conversation_id) SELECT id FROM conversations ON CONFLICT DO NOTHING;

CREATE OR REPLACE VIEW profile_message_months AS
SELECT c.profile_id, m.month, sum(m.sent)::bigint AS sent, sum(m.received)::bigint AS received
FROM conversation_message_months m JOIN conversations c ON c.id = m.conversation_id
WHERE c.conversation_type = 'direct' AND c.profile_id IS NOT NULL
GROUP BY c.profile_id, m.month;

CREATE OR REPLACE VIEW profile_contact_stats AS
SELECT c.profile_id, min(m.first_at) AS first_contact_at, max(m.last_at) AS last_contact_at,
  sum(m.sent + m.received)::bigint AS direct_message_count,
  sum(m.sent)::bigint AS sent_count, sum(m.received)::bigint AS received_count
FROM conversation_message_months m JOIN conversations c ON c.id = m.conversation_id
WHERE c.conversation_type = 'direct' AND c.profile_id IS NOT NULL
GROUP BY c.profile_id;
