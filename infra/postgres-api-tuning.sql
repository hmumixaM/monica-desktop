-- Interactive tuning for the NAS (Celeron-class CPU, 5.6 GiB RAM, RAID1 HDDs).
-- work_mem: aggregation and sort for dashboards/directory stay in memory
--   (16 MB x a handful of concurrent sorts fits the RAM budget).
-- jit: compiling mid-sized aggregates costs more than it saves on this CPU.
-- pg_stat_statements: records the slowest statements for later tuning (track=top by default).
-- Apply with psql, restart PostgreSQL once (shared_preload_libraries), then run
-- the CREATE EXTENSION line. Durability settings are unchanged.
ALTER SYSTEM SET work_mem = '16MB';
ALTER SYSTEM SET jit = off;
ALTER SYSTEM SET shared_preload_libraries = 'pg_stat_statements';
SELECT pg_reload_conf();
-- After the restart:
-- CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
SELECT name, setting, pending_restart FROM pg_settings
WHERE name IN ('work_mem', 'jit', 'shared_preload_libraries');
