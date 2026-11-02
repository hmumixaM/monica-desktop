ALTER TABLE activities ADD COLUMN IF NOT EXISTS state TEXT NOT NULL DEFAULT 'open'
  CHECK (state IN ('open','done'));
ALTER TABLE activities ADD COLUMN IF NOT EXISTS due_at TIMESTAMPTZ;
CREATE TABLE IF NOT EXISTS activity_revisions (
  sequence BIGSERIAL PRIMARY KEY,
  activity_id UUID NOT NULL REFERENCES activities(id),
  value JSONB NOT NULL,
  recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE OR REPLACE FUNCTION record_activity_revision() RETURNS trigger AS $$
BEGIN
  IF TG_OP='INSERT' OR (NEW.activity_type,NEW.occurred_at,NEW.title,NEW.body,NEW.state,NEW.due_at)
    IS DISTINCT FROM (OLD.activity_type,OLD.occurred_at,OLD.title,OLD.body,OLD.state,OLD.due_at) THEN
    INSERT INTO activity_revisions(activity_id,value) VALUES(NEW.id,to_jsonb(NEW));
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
INSERT INTO activity_revisions(activity_id,value)
SELECT a.id,to_jsonb(a) FROM activities a
WHERE NOT EXISTS(SELECT 1 FROM activity_revisions r WHERE r.activity_id=a.id);
DROP TRIGGER IF EXISTS activity_revision ON activities;
CREATE TRIGGER activity_revision AFTER INSERT OR UPDATE ON activities
FOR EACH ROW EXECUTE FUNCTION record_activity_revision();
CREATE INDEX IF NOT EXISTS activities_open_due ON activities(due_at) WHERE state='open' AND due_at IS NOT NULL;
