SET LOCAL lock_timeout = '5s';

-- Imported Stories already waiting at upgrade time need the same priority as
-- future imports. Do not touch running leases or historical terminal jobs.
UPDATE jobs SET priority = 10
WHERE job_type = 'ai.story_media_one' AND status = 'queued'
  AND payload->>'ingested' = 'true' AND priority < 10;
