# Exact message archive audit

Run these tools locally with the API Python environment. First freeze the source
SQLite files and verify their SHA-256 hashes against the NAS import snapshot.
Keep source files, the audit SQLite database, credentials and reports outside Git.

1. `message-source.py --root SOURCE_DIRECTORY --database AUDIT.sqlite3 --report source.json`
   constructs one expected record per conversation / stable message ID / original
   timestamp. It records source denominators and exclusions and refuses to replace
   an existing audit. Source files are opened read-only.
2. `message-projection.py --database AUDIT.sqlite3` streams message IDs, hashes,
   search-text hashes and ownership metadata from `nas` to the local audit. It
   reads `NAS_PASSWORD` only from the environment. PostgreSQL connections are
   read-only. Source message bodies are not exported by this step.
3. `message-compare.py --database AUDIT.sqlite3 --source-report source.json --report comparison.json`
   checks every expected body hash, search entry, message type and sender mapping.

The projection collector intentionally keeps copies local so it does not perform
millions of random joins on NAS disks. Run final collection after the selected
source import has stopped advancing its cursor; collection during import is a
partial checkpoint, not an atomic snapshot or a completion claim. Preserve extra
historical messages instead of deleting them to match one source snapshot.

Use `.local-validation/` for private reports. Run with
`uv run --project services/api python infra/audits/<script>.py ...` from the repo.

## Local WeChat media audit

Run `wechat-media.py --sns-db FIXED_SNS.db --cache /data/sns-cache --report REPORT.json`
inside the API environment after the archive pass finishes. The two PostgreSQL
snapshots are read-only; pause relevant archive writes for a final comparison.
The tool compares every source media slot with its exact local cache key, file
registration and stamp, existing asset file, CRM post slot and independent source
post slot. Nonzero exit means a projection discrepancy. Cache misses are recorded
separately as unresolved source gaps, even when the projection check exits zero;
that result never means all source pictures were recovered. This audit does not
download media or change records. Keep its detailed report outside Git.
