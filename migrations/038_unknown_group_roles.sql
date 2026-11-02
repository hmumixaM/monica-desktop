-- Source membership proves a person belongs to a group, but many archived
-- records do not state their role. Preserve unknown instead of inventing a
-- member/admin value. Group/person identity constraints remain unchanged.
ALTER TABLE source_group_memberships ALTER COLUMN role DROP NOT NULL;
