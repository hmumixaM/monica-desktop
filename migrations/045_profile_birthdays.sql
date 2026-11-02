-- One structured birthday per person. Parts stay null when that precision
-- does not know them, so a month-day is never stored as a fake year.
CREATE TABLE IF NOT EXISTS profile_birthdays (
  profile_id UUID PRIMARY KEY REFERENCES profiles(id) ON DELETE CASCADE,
  date_precision TEXT NOT NULL CHECK (date_precision IN ('month_day', 'approximate', 'exact')),
  year SMALLINT,
  month SMALLINT,
  day SMALLINT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_by UUID REFERENCES app_users(id) ON DELETE SET NULL,
  CONSTRAINT profile_birthdays_ranges CHECK (
    (year IS NULL OR year BETWEEN 1800 AND 2200)
    AND (month IS NULL OR month BETWEEN 1 AND 12)
    AND (day IS NULL OR day BETWEEN 1 AND 31)
  ),
  CONSTRAINT profile_birthdays_shape CHECK (
    (date_precision = 'exact' AND year IS NOT NULL AND month IS NOT NULL AND day IS NOT NULL)
    OR (date_precision = 'month_day' AND year IS NULL AND month IS NOT NULL AND day IS NOT NULL)
    OR (
      date_precision = 'approximate'
      AND (year IS NOT NULL OR month IS NOT NULL)
      AND (day IS NULL OR month IS NOT NULL)
    )
  )
);

CREATE INDEX IF NOT EXISTS profile_birthdays_month_day_idx
  ON profile_birthdays (month, day)
  WHERE month IS NOT NULL AND day IS NOT NULL;
