ALTER TABLE jobs ADD COLUMN dedupe_key text;
-- Historical terminal rows can keep NULL metadata; reads derive their lane.
-- Every insert/reactivation is classified by the trigger below.
ALTER TABLE jobs ADD COLUMN resource_class text;
ALTER TABLE jobs ADD COLUMN priority integer NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN lease_token uuid;
ALTER TABLE jobs ADD COLUMN lease_expires_at timestamptz;
ALTER TABLE jobs ADD COLUMN heartbeat_at timestamptz;
ALTER TABLE jobs ADD COLUMN rerun_requested boolean NOT NULL DEFAULT false;
CREATE FUNCTION job_subject(kind text, body jsonb) RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE
    WHEN left(kind,3)='ai.' THEN coalesce(body->>'profile_id',body->>'post_id',body->>'story_id',body->>'period','all')
    WHEN kind='media.dimensions' THEN body->>'media_id'
    WHEN kind='media.download' THEN encode(sha256(convert_to(jsonb_build_array(body->>'entity_type',body->>'entity_id',body->>'role',body->>'url')::text,'UTF8')),'hex')
    WHEN kind='metrics.refresh' THEN body->>'batch_id'
  END
$$;
CREATE FUNCTION job_resource(kind text) RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE WHEN left(kind,3)='ai.' THEN 'ai'
    WHEN kind='media.download' THEN 'media' ELSE 'db' END
$$;
CREATE FUNCTION populate_job_identity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.dedupe_key := job_subject(NEW.job_type, NEW.payload);
  NEW.resource_class := job_resource(NEW.job_type);
  RETURN NEW;
END $$;
CREATE TRIGGER job_identity BEFORE INSERT OR UPDATE OF job_type,payload,status ON jobs
FOR EACH ROW EXECUTE FUNCTION populate_job_identity();
-- Only executable work needs physical metadata. Recent failed AI subjects are
-- also populated for the existing seven-day retry exclusion. Rewriting all
-- completed payloads causes unnecessary TOAST/WAL I/O on the NAS.
UPDATE jobs SET payload=payload
WHERE status IN ('queued','running') OR
  (status='failed' AND left(job_type,3)='ai.' AND available_at>now()-interval '7 days');
WITH ranked AS (
  SELECT id, row_number() OVER (PARTITION BY job_type,dedupe_key
    ORDER BY CASE WHEN status='running' THEN 0 ELSE 1 END,available_at,id) AS n
  FROM jobs WHERE status IN ('queued','running') AND dedupe_key IS NOT NULL)
UPDATE jobs SET status='failed',error='Superseded duplicate during lease migration',completed_at=now()
WHERE id IN (SELECT id FROM ranked WHERE n>1);
CREATE UNIQUE INDEX jobs_active_subject ON jobs(job_type,dedupe_key)
WHERE status IN ('queued','running') AND dedupe_key IS NOT NULL;
CREATE INDEX jobs_resource_due ON jobs(resource_class,priority DESC,available_at,id)
WHERE status IN ('queued','running');
CREATE INDEX jobs_failed_subject ON jobs(job_type,dedupe_key,available_at) WHERE status='failed';
CREATE TABLE worker_heartbeats (
 worker_id text PRIMARY KEY, resource_class text NOT NULL, heartbeat_at timestamptz NOT NULL DEFAULT now(),
 job_id uuid, draining boolean NOT NULL DEFAULT false
);
CREATE TABLE model_call_slots (name text PRIMARY KEY, next_at timestamptz NOT NULL);
