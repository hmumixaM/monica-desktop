ALTER TABLE face_scans ADD COLUMN status text NOT NULL DEFAULT 'success'
  CHECK(status IN ('success','retryable_error','permanent_error'));
ALTER TABLE face_scans ADD COLUMN error text;
ALTER TABLE face_scans ADD COLUMN next_attempt_at timestamptz;
UPDATE face_scans SET status='retryable_error', error=split_part(model, ':', 2), next_attempt_at=now()
WHERE model ~ ':(timeout|missing|unreadable)$';
ALTER TABLE social_post_media_readings ADD COLUMN source_caption text;
ALTER TABLE social_post_media_readings ADD COLUMN prompt_hash text;
ALTER TABLE social_story_media_readings ADD COLUMN source_caption text;
ALTER TABLE social_story_media_readings ADD COLUMN prompt_hash text;
