-- Reloadable settings for the NAS mechanical-disk volume. WAL remains durable;
-- fsync and full_page_writes retain PostgreSQL defaults. A longer checkpoint
-- interval avoids repeatedly rewriting full pages during the initial archive.
ALTER SYSTEM SET max_wal_size = '4GB';
ALTER SYSTEM SET checkpoint_timeout = '15min';
ALTER SYSTEM SET wal_compression = 'on';
SELECT pg_reload_conf();
