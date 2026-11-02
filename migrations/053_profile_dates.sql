-- Significant dates per person: the birthday (at most one) plus anniversaries and any other dates worth keeping.
-- Precision keeps the birthday rules: exact, month and day, or an approximate partial date.
-- (No percent signs: migrations also run through raw driver execution.)
CREATE TABLE IF NOT EXISTS profile_dates (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  profile_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('birthday', 'anniversary', 'other')),
  label TEXT CHECK (label IS NULL OR char_length(label) BETWEEN 1 AND 60),
  date_precision TEXT NOT NULL CHECK (date_precision IN ('month_day', 'approximate', 'exact')),
  year SMALLINT,
  month SMALLINT,
  day SMALLINT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_by UUID REFERENCES app_users(id) ON DELETE SET NULL,
  CONSTRAINT profile_dates_ranges CHECK (
    (year IS NULL OR year BETWEEN 1800 AND 2200)
    AND (month IS NULL OR month BETWEEN 1 AND 12)
    AND (day IS NULL OR day BETWEEN 1 AND 31)
  ),
  CONSTRAINT profile_dates_shape CHECK (
    (date_precision = 'exact' AND year IS NOT NULL AND month IS NOT NULL AND day IS NOT NULL)
    OR (date_precision = 'month_day' AND year IS NULL AND month IS NOT NULL AND day IS NOT NULL)
    OR (
      date_precision = 'approximate'
      AND (year IS NOT NULL OR month IS NOT NULL)
      AND (day IS NULL OR month IS NOT NULL)
    )
  )
);

CREATE UNIQUE INDEX IF NOT EXISTS profile_dates_one_birthday ON profile_dates (profile_id) WHERE kind = 'birthday';
CREATE INDEX IF NOT EXISTS profile_dates_profile ON profile_dates (profile_id);
CREATE INDEX IF NOT EXISTS profile_dates_month_day ON profile_dates (month, day)
  WHERE month IS NOT NULL AND day IS NOT NULL;

DO $$
BEGIN
  IF to_regclass('profile_birthdays') IS NOT NULL THEN
    INSERT INTO profile_dates (profile_id, kind, date_precision, year, month, day, created_at, updated_at, updated_by)
    SELECT profile_id, 'birthday', date_precision, year, month, day, updated_at, updated_at, updated_by
    FROM profile_birthdays
    ON CONFLICT DO NOTHING;
    DROP TABLE profile_birthdays;
  END IF;
END $$;

-- Releases before this migration read profile_birthdays; they keep running during the blue-green overlap
-- and remain the rollback target, so they get a read-only view of the birthdays.
CREATE OR REPLACE VIEW profile_birthdays AS
  SELECT profile_id, date_precision, year, month, day, updated_at, updated_by
  FROM profile_dates WHERE kind = 'birthday';

DROP TRIGGER IF EXISTS data_version_insert ON profile_dates;
DROP TRIGGER IF EXISTS data_version_update ON profile_dates;
DROP TRIGGER IF EXISTS data_version_delete ON profile_dates;
DROP TRIGGER IF EXISTS data_version_truncate ON profile_dates;
CREATE TRIGGER data_version_insert AFTER INSERT ON profile_dates
  REFERENCING NEW TABLE AS changed_rows FOR EACH STATEMENT EXECUTE FUNCTION bump_data_version('profiles');
CREATE TRIGGER data_version_update AFTER UPDATE ON profile_dates
  REFERENCING NEW TABLE AS changed_rows FOR EACH STATEMENT EXECUTE FUNCTION bump_data_version('profiles');
CREATE TRIGGER data_version_delete AFTER DELETE ON profile_dates
  REFERENCING OLD TABLE AS changed_rows FOR EACH STATEMENT EXECUTE FUNCTION bump_data_version('profiles');
CREATE TRIGGER data_version_truncate AFTER TRUNCATE ON profile_dates
  FOR EACH STATEMENT EXECUTE FUNCTION bump_data_version_truncate('profiles');
