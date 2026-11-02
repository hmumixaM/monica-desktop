-- A tag the person using the CRM removed from a profile stays removed: importers and account-link
-- projections skip these pairs. Adding the tag back by hand deletes the row.
CREATE TABLE IF NOT EXISTS tag_membership_removals (
  tag_id UUID NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  profile_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  removed_by UUID REFERENCES app_users(id) ON DELETE SET NULL,
  removed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tag_id, profile_id)
);
