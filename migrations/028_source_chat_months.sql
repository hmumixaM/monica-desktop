CREATE TABLE source_chat_months (
  account_id UUID NOT NULL REFERENCES source_accounts(id),
  month DATE NOT NULL,
  sent BIGINT NOT NULL CHECK(sent>=0),
  received BIGINT NOT NULL CHECK(received>=0),
  total BIGINT NOT NULL CHECK(total=sent+received),
  first_message_at TIMESTAMPTZ,
  last_message_at TIMESTAMPTZ,
  snapshot_at TIMESTAMPTZ NOT NULL,
  timezone TEXT NOT NULL,
  PRIMARY KEY(account_id,month)
);
