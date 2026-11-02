SET LOCAL lock_timeout = '5s';

ALTER TABLE image_ocr_readings ADD COLUMN description TEXT NOT NULL DEFAULT '';
ALTER TABLE image_ocr_readings ADD COLUMN category TEXT NOT NULL DEFAULT '';
ALTER TABLE image_ocr_readings ADD COLUMN summary TEXT NOT NULL DEFAULT '';

-- Retain readable OCR and all quota/attempt accounting. Completed text-only
-- work is repaired by the same paced worker, newest first, rather than by an
-- unbounded second scan. Pending/running work will use the combined schema.
UPDATE image_ocr_readings SET status='pending',available_at=now()
WHERE status='complete';
