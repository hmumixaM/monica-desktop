-- A hand-kept detail can be deleted: the revision stores a null value, so imports cannot bring it back.
-- NOT VALID: every existing row already passes the narrower old check, and rescanning all revisions on the NAS is slow.
ALTER TABLE profile_field_revisions DROP CONSTRAINT IF EXISTS profile_field_revisions_operation_check;
ALTER TABLE profile_field_revisions ADD CONSTRAINT profile_field_revisions_operation_check
  CHECK (operation IN ('import', 'manual_edit', 'manual_delete', 'restore', 'accept_import')) NOT VALID;

-- Uploaded documents keep the name they were uploaded with; photos and imports leave it empty.
ALTER TABLE media_assets ADD COLUMN IF NOT EXISTS file_name TEXT;
