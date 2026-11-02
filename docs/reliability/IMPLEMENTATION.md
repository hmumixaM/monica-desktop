# Monica Next reliability, performance and release implementation

This series implements the approved 2026-10-16 review plan on top of main
`4a25b5b077544199f8ed856463a2318a5258d20e`, after comparing the review baseline
`d5795ebe`. Existing visual-context work on main is retained. The original
checkout and its 75 untracked copies were not modified.

## Review-to-code map

| Review | Implementation | Regression evidence |
|---|---|---|
| 1: one worker blocks unrelated tasks | Separate scheduler, DB worker, AI worker, two media workers, with attachment-slot claim exclusion; actual-call limiter and bounded child process lifetime | Queue lane/lease tests; real worker subprocess test |
| 2: prefetch failure aborts archive cycle | Independent source/media/projection/warmup stages, stage status, pending-backlog retry | Archive failure/pending-shard tests |
| 3: sequence exposes uncommitted changes | Transactional invalidation events, atomic count compaction | Uncommitted write, rollback, reverse commit order, compaction test |
| 4: lost/expired task ownership | Fresh token per claim, 30-second heartbeat, two-minute lease, commit fence, process budget, five-attempt cap | Stale ack and stale result rollback; real child execution |
| 5: LinkedIn busy and pooled advisory lock | Explicit busy/unchanged/success results, pinned physical lock connection, backoff | Existing LinkedIn import/replay tests; physical connection code path |
| 6: AI enqueue races | Active subject unique index, conflict-safe enqueue, serialized spacing reservation | Four concurrent enqueuers and queued/running refresh tests |
| 7: face failures marked complete | Per-scan status/model/error, due retry, zero faces remains success, recognizable legacy-error migration | Retry vs zero-face and old-client compatibility tests |
| 8: process-global IO nesting | Reentrant thread lock around process flock; worker and importer share staging mount | Two-thread exclusion test |
| 9: cache stampede and unconstrained refresh | Per-key singleflight, one refresh worker, interactive query timeout, logged failures | Concurrent cold miss and byte-budget tests |
| 10: AI caption/model/prompt changes | Candidate comparison includes all three; input hash still avoids duplicate calls | Caption/model/prompt revisions and unchanged-input tests |
| 11: message key timezone | UTC helper, exact source-ID/account/time legacy lookup, ambiguity refusal, read-only audit | UTC/Chicago identity tests; both complete suite variants |

The cache version is `compacted_count + visible_event_count`, read in one SQL
snapshot. This deliberately replaces the planned `(count,max_id)` fingerprint
with an exact monotonic count: compaction preserves the value and a late commit
always increments it, regardless of sequence allocation order. Writers never
update a global version row. Old sequence triggers remain compatible during the
API overlap period; new cache files use format 2.

## Performance, memory and simplification

* LinkedIn media enqueue is restricted to changed accounts, and shares video/poster
  handling with the normal importer. The full scan remains available through the
  historical migration command. JSON parsing avoids the extra original string;
  export hashing is incremental.
* Profile media refresh updates only media projections. Relinking still performs
  a full rebuild and preserves manual-field and avatar-precedence policies.
* Home invalidation depends only on profiles/messages. Cache memory is capped at
  512 entries and an estimated 64 MiB, with a 4 MiB single-entry ceiling.
* Face tar output reads one 256 KiB chunk at a time, closes its descriptor on
  cancellation, and does not prefetch files. The allocation regression processes
  128 MiB of sparse fixture media with a peak allocation below 2 MiB.
* Image processing requests JPEG decode reduction and shrinks before conversion.
* Group speech pages ten rooms, loads at most 200 stored messages per selected
  room, shares a 15-second request budget, permits two upstream requests, and
  returns a resumable window position with partial results.
* Story checkpoints flush every 50 commits or five seconds, and at a normal stop.
* Source projection helpers are grouped by profile/social/message; incremental
  LinkedIn no longer imports its normalizer from the historical migration.
  Shared query functions remove the overview/warmup dependency on `main`.
* API and background pool sizes are explicit. Existing memory limits have not
  been represented as measured recommendations. Archive sync uses four connections
  to accommodate its source locks, business transaction and heartbeat independently.

## Operational contract

`/api/v1/jobs/status` and Settings show queue state, oldest due work, expired
leases, worker heartbeats (gracefully stopped processes are excluded), source failures, last success and next retry. A
scheduler's completion describes dispatch, not completion of all child work.
Model calls, downloads and disk-lock waits do not retain unrelated transactions.
Model retries reserve another actual-call slot (profile 180 seconds, reconnect 30 seconds, other calls 12 seconds by default; configurable). Child processes enforce execution
budgets (AI 900 seconds; other tasks 300 seconds by default).

Final single-subject AI results and task acknowledgement commit together. Batch
jobs may checkpoint completed child dispatches or model outputs; each checkpoint
is fenced and replay uses existing input hashes. External calls are at least
once after faults. Cross-store media writes hold the CRM ownership row during
final materialization, so a lease takeover cannot race that short commit phase.
CRM-only result transactions defer the row lock until final acknowledgement;
their independent heartbeat can renew throughout a slow result query. Lease
bootstrap only fills active/recent retry metadata, leaving terminal history
untouched; reactivation triggers populate its identity/lane. Both behaviors have
PostgreSQL regressions, including a pre-fix heartbeat lock-timeout reproduction.
A media finalization lock timeout only permits a read of the still-valid committed
lease; this does not extend expiry or bypass execution budgets. Rejected commit
fences roll back the DBAPI transaction (or invalidate the connection) before pool
reuse, including manual commit paths. LinkedIn media archival also checks shutdown after waiting for disk permission, flushes pending cross-store links, and commits only completed units before exiting. Source cursors stay unchanged on interruption; PostgreSQL regressions cover durable partial replay and pending post links.

## Explicit rollout gates and remaining acceptance work

These gates are **not completed production acceptance**:

1. `MONICA_SCOPED_OWNERSHIP_LOCKS` is **off by default**. Ordered account/profile
   locks are present, but the existing ownership barrier stays in place until
   the full cross-source/group relinking concurrency matrix is validated. Ordinary
   source importers already share this barrier; unrelated worker resource lanes
   run independently. Do not enable this flag simply because unit tests pass.
2. Representative business-page p95/p99, query plans and memory/swap comparisons
   under the same importer/model/media load remain unmeasured. Privileged NAS
   inspection, private GHCR publication/read-only pulls, UTC key auditing and
   schema migration are now verified; release evidence and actual runtime checks
   were kept in private deployment records, which are not included in this public source snapshot.
3. The backup inventory found CRM-era dumps/config copies, not evidence of a
   verified current four-database/media recovery set. External NAS backup tasks
   and snapshots remain unverified. The isolated restore tool validates file
   checksums, source accounts/posts/payload references and media bytes, but an
   actual full backup/restore rehearsal has not run.
4. Query indexes and memory limits need representative production EXPLAIN/load
   evidence. The benchmark tool is provided; no synthetic result is presented
   as a production performance gain.
5. Collector code, snapshot publication and compatible API must be rolled out in
   the documented order. Do not infer a failed collector from an old publication
   timestamp or restart an intentionally paused collector.

The private runtime snapshots were historical, pre-privilege snapshots
of the review baseline, including an unverified state-file release `4a25b5b`.
They do not identify the current production release. The deployment record links
CI/manifest provenance to Docker/Caddy inspection and preserves failed-attempt
recovery evidence. NAS Stories retain eight hours and archives six hours; Mac
publication remains daily and its underlying collectors every three hours.
No collector was restarted or schedule reset by the NAS rollout.
