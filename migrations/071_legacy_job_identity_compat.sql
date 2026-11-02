-- Forward compatibility for databases which already applied the first 067.
-- No historical payload rewrite is needed for lane display or reactivation.
ALTER TABLE jobs ALTER COLUMN resource_class DROP NOT NULL;
ALTER TABLE jobs ALTER COLUMN resource_class DROP DEFAULT;
CREATE OR REPLACE FUNCTION job_resource(kind text) RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE WHEN left(kind,3)='ai.' THEN 'ai'
    WHEN kind='media.download' THEN 'media' ELSE 'db' END
$$;
CREATE OR REPLACE FUNCTION populate_job_identity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.dedupe_key := job_subject(NEW.job_type, NEW.payload);
  NEW.resource_class := job_resource(NEW.job_type);
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS job_identity ON jobs;
CREATE TRIGGER job_identity BEFORE INSERT OR UPDATE OF job_type,payload,status ON jobs
FOR EACH ROW EXECUTE FUNCTION populate_job_identity();
