-- /search matches activity titles and bodies with ILIKE; trigram indexes keep
-- that from scanning every activity. Post and story search bodies are already
-- indexed (017) once the query no longer ORs in an unindexed provider match.
CREATE INDEX IF NOT EXISTS activities_title_trgm ON activities USING gin (title gin_trgm_ops);
CREATE INDEX IF NOT EXISTS activities_body_trgm ON activities USING gin (body gin_trgm_ops);
