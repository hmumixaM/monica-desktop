-- Current pointers are explicit, so quarantined historical values cannot be
-- resurrected when an account is linked again.
CREATE TABLE source_account_fields (
  account_id UUID NOT NULL REFERENCES source_accounts(id), field_key TEXT NOT NULL,
  revision_id UUID NOT NULL REFERENCES profile_field_revisions(id),
  PRIMARY KEY(account_id,field_key)
);
CREATE TABLE source_account_facts (
  account_id UUID NOT NULL REFERENCES source_accounts(id), fact_type TEXT NOT NULL,
  fact_key TEXT NOT NULL, revision_id UUID NOT NULL,
  PRIMARY KEY(account_id,fact_type,fact_key)
);
CREATE TABLE source_account_media (
  account_id UUID NOT NULL REFERENCES source_accounts(id), media_id UUID NOT NULL REFERENCES media_assets(id),
  role TEXT NOT NULL, PRIMARY KEY(account_id,media_id,role)
);
CREATE TABLE source_account_tags (
  account_id UUID NOT NULL REFERENCES source_accounts(id), tag_id UUID NOT NULL REFERENCES tags(id),
  PRIMARY KEY(account_id,tag_id)
);
CREATE TABLE platform_migration_state (
  provider TEXT PRIMARY KEY CHECK(provider IN ('wechat','instagram','linkedin')),
  enabled BOOLEAN NOT NULL DEFAULT false, cursor JSONB NOT NULL DEFAULT '{}',
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE media_links ADD COLUMN source_account_id UUID REFERENCES source_accounts(id);
CREATE INDEX media_links_account ON media_links(source_account_id);
ALTER TABLE relationship_edges ADD COLUMN from_account_id UUID REFERENCES source_accounts(id);
ALTER TABLE relationship_edges ADD COLUMN to_account_id UUID REFERENCES source_accounts(id);
CREATE INDEX relationship_from_account ON relationship_edges(from_account_id);
CREATE INDEX relationship_to_account ON relationship_edges(to_account_id);
CREATE INDEX source_account_revisions_account ON source_account_link_revisions(account_id,id);
CREATE TABLE source_relationship_edges (
  id UUID PRIMARY KEY, from_account_id UUID NOT NULL REFERENCES source_accounts(id),
  to_account_id UUID NOT NULL REFERENCES source_accounts(id),
  relationship_type_id UUID NOT NULL REFERENCES relationship_types(id),
  note TEXT, confidence NUMERIC(5,4),valid_from TIMESTAMPTZ,valid_to TIMESTAMPTZ,
  source_type TEXT NOT NULL,evidence_kind TEXT NOT NULL
);
-- Rebuildable history indexes may belong to an unlinked source account.
ALTER TABLE profile_field_revisions ALTER COLUMN profile_id DROP NOT NULL;
ALTER TABLE profile_address_revisions ALTER COLUMN profile_id DROP NOT NULL;
ALTER TABLE profile_employment_revisions ALTER COLUMN profile_id DROP NOT NULL;
ALTER TABLE profile_education_revisions ALTER COLUMN profile_id DROP NOT NULL;
