-- The same date recorded twice for one person is a repeated submission, not a second date:
-- adding it again returns the existing row. Earlier duplicates keep their oldest copy.
DELETE FROM profile_dates d USING profile_dates older
WHERE d.profile_id = older.profile_id AND d.kind = older.kind
  AND COALESCE(d.label, '') = COALESCE(older.label, '') AND d.date_precision = older.date_precision
  AND COALESCE(d.year, 0) = COALESCE(older.year, 0) AND COALESCE(d.month, 0) = COALESCE(older.month, 0)
  AND COALESCE(d.day, 0) = COALESCE(older.day, 0)
  AND (d.created_at, d.id) > (older.created_at, older.id);

CREATE UNIQUE INDEX IF NOT EXISTS profile_dates_distinct ON profile_dates (
  profile_id, kind, COALESCE(label, ''), date_precision, COALESCE(year, 0), COALESCE(month, 0), COALESCE(day, 0));
