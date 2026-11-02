-- Rebuildable, account-owned summary/detail index. Immutable observations and
-- complete original rows live in the independent WeChat database.
CREATE TABLE source_contact_events (
  account_id UUID NOT NULL REFERENCES source_accounts(id),
  event_kind TEXT NOT NULL CHECK(event_kind IN ('call','money')),
  external_id TEXT NOT NULL,
  occurred_at TIMESTAMPTZ,
  snapshot_at TIMESTAMPTZ NOT NULL,
  content JSONB NOT NULL,
  PRIMARY KEY(account_id,event_kind,external_id)
);
CREATE INDEX source_contact_events_timeline ON source_contact_events(account_id,event_kind,occurred_at DESC,external_id);
