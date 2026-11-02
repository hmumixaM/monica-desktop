-- Last contact also counts a person's own lines in groups: some people mostly talk in
-- shared groups. contact_stats.refresh keeps one row per conversation and sender; views
-- resolve senders through their current account link, as direct statistics do.
SET LOCAL lock_timeout = '5s';

CREATE TABLE IF NOT EXISTS conversation_sender_stats (
  conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  sender_account_id UUID REFERENCES source_accounts(id) ON DELETE CASCADE,
  -- Only for legacy rows without an account.
  sender_profile_id UUID REFERENCES profiles(id) ON DELETE CASCADE,
  messages BIGINT NOT NULL,
  last_at TIMESTAMPTZ NOT NULL,
  CHECK (sender_account_id IS NOT NULL OR sender_profile_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS conversation_sender_stats_conversation ON conversation_sender_stats(conversation_id);

DROP TRIGGER IF EXISTS data_version_insert ON conversation_sender_stats;
DROP TRIGGER IF EXISTS data_version_delete ON conversation_sender_stats;
CREATE TRIGGER data_version_insert AFTER INSERT ON conversation_sender_stats
  REFERENCING NEW TABLE AS changed_rows FOR EACH STATEMENT EXECUTE FUNCTION bump_data_version('messages');
CREATE TRIGGER data_version_delete AFTER DELETE ON conversation_sender_stats
  REFERENCING OLD TABLE AS changed_rows FOR EACH STATEMENT EXECUTE FUNCTION bump_data_version('messages');

INSERT INTO conversation_stats_dirty(conversation_id) SELECT id FROM conversations ON CONFLICT DO NOTHING;

-- The other side of a direct chat is always us; an account that does so in more than one
-- direct chat is ours, so a stray message in someone else's chat does not hide them.
CREATE OR REPLACE VIEW own_message_accounts AS
SELECT s.sender_account_id AS account_id
FROM conversation_sender_stats s JOIN conversations c ON c.id = s.conversation_id
WHERE c.conversation_type = 'direct' AND s.sender_account_id <> c.source_account_id
GROUP BY s.sender_account_id HAVING count(*) > 1;

CREATE OR REPLACE VIEW profile_group_speech AS
SELECT speech.profile_id, speech.conversation_id, speech.messages, speech.last_at
FROM (SELECT coalesce(l.profile_id, s.sender_profile_id) AS profile_id, s.conversation_id, s.messages, s.last_at
      FROM conversation_sender_stats s
      JOIN conversations c ON c.id = s.conversation_id AND c.conversation_type = 'group'
      LEFT JOIN source_account_links l ON l.account_id = s.sender_account_id) speech
WHERE speech.profile_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM own_message_accounts o JOIN source_account_links own ON own.account_id = o.account_id
                  WHERE own.profile_id = speech.profile_id);

-- Grouped over a union, so a filter on profile_id reaches both sides.
CREATE OR REPLACE VIEW profile_contact_stats AS
SELECT profile_id, min(first_direct_at) AS first_contact_at, max(last_at) AS last_contact_at,
  sum(direct)::bigint AS direct_message_count, sum(sent)::bigint AS sent_count, sum(received)::bigint AS received_count,
  max(last_direct_at) AS last_direct_at, max(last_group_at) AS last_group_at,
  sum(group_messages)::bigint AS group_message_count
FROM (
  SELECT c.profile_id, m.first_at AS first_direct_at, m.last_at, m.sent + m.received AS direct, m.sent, m.received,
    m.last_at AS last_direct_at, NULL::timestamptz AS last_group_at, 0::bigint AS group_messages
  FROM conversation_message_months m JOIN conversations c ON c.id = m.conversation_id
  WHERE c.conversation_type = 'direct' AND c.profile_id IS NOT NULL
  UNION ALL
  SELECT profile_id, NULL, last_at, 0, 0, 0, NULL, last_at, messages FROM profile_group_speech
) contact
GROUP BY profile_id;
