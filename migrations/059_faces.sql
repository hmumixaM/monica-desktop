-- Faces found by the home Mac's local model. Embeddings stay on that machine; Monica keeps the box,
-- the detector's confidence, the model's suggestion and the person's decision.
CREATE TABLE IF NOT EXISTS faces (
  id UUID PRIMARY KEY,
  media_id UUID NOT NULL REFERENCES media_assets(id) ON DELETE CASCADE,
  -- Left, top, width, height as fractions of the image.
  box REAL[] NOT NULL CHECK (array_length(box, 1) = 4),
  quality REAL NOT NULL,
  -- Faces in post photos are reviewed on /faces; avatar faces only inform account linking.
  in_post BOOLEAN NOT NULL DEFAULT false,
  cluster_key TEXT,
  suggested_profile_id UUID REFERENCES profiles(id) ON DELETE SET NULL,
  suggestion_score REAL,
  -- "Not this person" keeps a face pending and records whom it is not, so that suggestion is not made again.
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'confirmed', 'ignored')),
  profile_id UUID REFERENCES profiles(id) ON DELETE SET NULL,
  rejected_profile_id UUID REFERENCES profiles(id) ON DELETE SET NULL,
  detected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  decided_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS faces_media ON faces(media_id);
CREATE INDEX IF NOT EXISTS faces_confirmed ON faces(profile_id, media_id) WHERE status = 'confirmed';
CREATE INDEX IF NOT EXISTS faces_review ON faces(suggested_profile_id, cluster_key) WHERE status = 'pending' AND in_post;
CREATE INDEX IF NOT EXISTS faces_suggested ON faces(suggested_profile_id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS faces_decided ON faces(decided_at) WHERE decided_at IS NOT NULL;

-- Images the model has looked at, including those with no face.
CREATE TABLE IF NOT EXISTS face_scans (
  media_id UUID PRIMARY KEY REFERENCES media_assets(id) ON DELETE CASCADE,
  model TEXT NOT NULL,
  faces INTEGER NOT NULL,
  scanned_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE SEQUENCE IF NOT EXISTS data_version_faces;
DO $$
DECLARE
  relation text;
BEGIN
  FOREACH relation IN ARRAY ARRAY['faces', 'face_scans'] LOOP
    EXECUTE 'DROP TRIGGER IF EXISTS data_version_insert ON ' || relation;
    EXECUTE 'DROP TRIGGER IF EXISTS data_version_update ON ' || relation;
    EXECUTE 'DROP TRIGGER IF EXISTS data_version_delete ON ' || relation;
    EXECUTE 'CREATE TRIGGER data_version_insert AFTER INSERT ON ' || relation
      || ' REFERENCING NEW TABLE AS changed_rows FOR EACH STATEMENT EXECUTE FUNCTION bump_data_version(''faces'')';
    EXECUTE 'CREATE TRIGGER data_version_update AFTER UPDATE ON ' || relation
      || ' REFERENCING NEW TABLE AS changed_rows FOR EACH STATEMENT EXECUTE FUNCTION bump_data_version(''faces'')';
    EXECUTE 'CREATE TRIGGER data_version_delete AFTER DELETE ON ' || relation
      || ' REFERENCING OLD TABLE AS changed_rows FOR EACH STATEMENT EXECUTE FUNCTION bump_data_version(''faces'')';
  END LOOP;
END $$;
