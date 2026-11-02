CREATE TABLE IF NOT EXISTS companies (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name TEXT NOT NULL,
  normalized_name TEXT NOT NULL UNIQUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS schools (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name TEXT NOT NULL,
  normalized_name TEXT NOT NULL UNIQUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS profile_address_revisions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  profile_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  content_object_id UUID NOT NULL REFERENCES content_objects(id),
  content_hash CHAR(64) NOT NULL,
  source_type TEXT NOT NULL,
  source_record_id TEXT,
  actor_user_id UUID REFERENCES app_users(id) ON DELETE SET NULL,
  observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  previous_revision_id UUID REFERENCES profile_address_revisions(id),
  is_conflict BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS profile_address_revisions_idx
  ON profile_address_revisions(profile_id, observed_at DESC);

CREATE TABLE IF NOT EXISTS profile_employment_revisions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  profile_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  company_id UUID REFERENCES companies(id) ON DELETE SET NULL,
  content_object_id UUID NOT NULL REFERENCES content_objects(id),
  content_hash CHAR(64) NOT NULL,
  source_type TEXT NOT NULL,
  source_record_id TEXT,
  actor_user_id UUID REFERENCES app_users(id) ON DELETE SET NULL,
  observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  previous_revision_id UUID REFERENCES profile_employment_revisions(id),
  is_conflict BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS profile_employment_revisions_idx
  ON profile_employment_revisions(profile_id, observed_at DESC);

CREATE TABLE IF NOT EXISTS profile_education_revisions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  profile_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  school_id UUID REFERENCES schools(id) ON DELETE SET NULL,
  content_object_id UUID NOT NULL REFERENCES content_objects(id),
  content_hash CHAR(64) NOT NULL,
  source_type TEXT NOT NULL,
  source_record_id TEXT,
  actor_user_id UUID REFERENCES app_users(id) ON DELETE SET NULL,
  observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  previous_revision_id UUID REFERENCES profile_education_revisions(id),
  is_conflict BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS profile_education_revisions_idx
  ON profile_education_revisions(profile_id, observed_at DESC);

CREATE TABLE IF NOT EXISTS profile_structured_current (
  profile_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  fact_type TEXT NOT NULL CHECK (fact_type IN ('address', 'employment', 'education')),
  current_revision_id UUID NOT NULL,
  current_content_hash CHAR(64) NOT NULL,
  current_source_type TEXT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY(profile_id, fact_type)
);
