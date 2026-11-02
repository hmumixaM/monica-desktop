-- Explicit opt-in collection is independent of LinkedIn connection status.
-- Account ownership continues to use the existing reversible source links.
CREATE TABLE linkedin_collection_targets (
  account_id UUID PRIMARY KEY REFERENCES source_accounts(id),
  created_by UUID REFERENCES app_users(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
