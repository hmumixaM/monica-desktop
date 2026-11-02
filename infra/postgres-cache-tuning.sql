-- NAS has 5.6 GiB RAM and about 2.8 GiB available during archive ingestion.
-- The hot message/content indexes alone exceed 600 MiB. PostgreSQL's default
-- 128 MiB buffer repeatedly evicts them and blocks readers behind dirty writes.
-- Apply after pausing resumable imports at their committed checkpoints, then
-- restart PostgreSQL once. Restore 128MB with ALTER SYSTEM if memory is tight.
-- This setting does not weaken WAL durability, fsync, or full_page_writes.
ALTER SYSTEM SET shared_buffers = '768MB';
SELECT pg_reload_conf();
SELECT name,setting,unit,pending_restart FROM pg_settings WHERE name='shared_buffers';
