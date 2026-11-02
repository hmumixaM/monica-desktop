-- Source attribution updates and metadata dashboards must not repeatedly scan
-- every current field or touch unrelated message content on a cold NAS cache.
CREATE INDEX IF NOT EXISTS field_current_revision_idx ON profile_field_current(current_revision_id);
CREATE INDEX IF NOT EXISTS structured_current_revision_idx ON profile_structured_current(current_revision_id);
CREATE INDEX IF NOT EXISTS field_current_key_idx ON profile_field_current(field_key text_pattern_ops,profile_id);
CREATE INDEX IF NOT EXISTS repair_current_revision_idx ON repair_snapshots((payload->>'current_revision_id'))
  WHERE entity_type='profile_field_current';
