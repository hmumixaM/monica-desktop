-- Registry and reversible human-confirmed links. Source payloads live in the
-- three platform databases; these IDs identify accounts, never people by name.
CREATE TABLE source_accounts (
  id UUID PRIMARY KEY,
  provider TEXT NOT NULL CHECK(provider IN ('wechat','instagram','linkedin')),
  external_id TEXT NOT NULL,
  object_kind TEXT NOT NULL CHECK(object_kind IN ('person','group')),
  display_name TEXT NOT NULL,
  username TEXT,
  profile_url TEXT,
  migrated_at TIMESTAMPTZ,
  UNIQUE(provider,external_id)
);
CREATE TABLE source_account_links (
  account_id UUID PRIMARY KEY REFERENCES source_accounts(id),
  profile_id UUID NOT NULL REFERENCES profiles(id),
  evidence JSONB NOT NULL,
  linked_by UUID REFERENCES app_users(id),
  linked_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX source_account_links_profile ON source_account_links(profile_id,account_id);
CREATE TABLE source_account_link_revisions (
  id BIGSERIAL PRIMARY KEY,
  account_id UUID NOT NULL REFERENCES source_accounts(id),
  old_profile_id UUID REFERENCES profiles(id),
  new_profile_id UUID REFERENCES profiles(id),
  actor_user_id UUID REFERENCES app_users(id),
  evidence JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE identities ADD COLUMN source_account_id UUID REFERENCES source_accounts(id);
ALTER TABLE social_posts ADD COLUMN source_account_id UUID REFERENCES source_accounts(id);
ALTER TABLE social_stories ADD COLUMN source_account_id UUID REFERENCES source_accounts(id);
ALTER TABLE social_interactions ADD COLUMN author_account_id UUID REFERENCES source_accounts(id);
ALTER TABLE conversations ADD COLUMN source_account_id UUID REFERENCES source_accounts(id);
ALTER TABLE message_events ADD COLUMN sender_account_id UUID REFERENCES source_accounts(id);
ALTER TABLE profile_field_revisions ADD COLUMN source_account_id UUID REFERENCES source_accounts(id);
ALTER TABLE profile_field_current ADD COLUMN source_account_id UUID REFERENCES source_accounts(id);
ALTER TABLE profile_structured_current ADD COLUMN source_account_id UUID REFERENCES source_accounts(id);
ALTER TABLE profile_address_revisions ADD COLUMN source_account_id UUID REFERENCES source_accounts(id);
ALTER TABLE profile_employment_revisions ADD COLUMN source_account_id UUID REFERENCES source_accounts(id);
ALTER TABLE profile_education_revisions ADD COLUMN source_account_id UUID REFERENCES source_accounts(id);
ALTER TABLE tag_memberships ADD COLUMN source_account_id UUID REFERENCES source_accounts(id);
CREATE INDEX social_posts_account ON social_posts(source_account_id,id);
CREATE INDEX social_stories_account ON social_stories(source_account_id,id);
CREATE INDEX social_interactions_account ON social_interactions(author_account_id,id);
CREATE INDEX conversations_account ON conversations(source_account_id,id);
CREATE INDEX field_revisions_account ON profile_field_revisions(source_account_id,revision_seq);
CREATE INDEX field_current_account ON profile_field_current(source_account_id,field_key);
CREATE INDEX structured_current_account ON profile_structured_current(source_account_id,fact_type,fact_key);
CREATE INDEX identities_account ON identities(source_account_id,id);
CREATE TABLE source_profile_projections (
  account_id UUID NOT NULL REFERENCES source_accounts(id),
  object_kind TEXT NOT NULL,
  source_object_id UUID NOT NULL,
  projection_kind TEXT NOT NULL,
  projection_id TEXT NOT NULL,
  PRIMARY KEY(account_id,source_object_id,projection_kind,projection_id)
);
CREATE TABLE source_group_memberships (
  group_account_id UUID NOT NULL REFERENCES source_accounts(id),
  person_account_id UUID NOT NULL REFERENCES source_accounts(id),
  role TEXT NOT NULL DEFAULT 'member',
  joined_at TIMESTAMPTZ,
  left_at TIMESTAMPTZ,
  source_record_id TEXT,
  PRIMARY KEY(group_account_id,person_account_id),
  CHECK(group_account_id<>person_account_id)
);
