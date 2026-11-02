> Legacy server runbook. The public desktop CI does not publish images or deploy a NAS; these steps apply only to a separately configured server deployment.

# Rollout and recovery runbook

## Before release

Run the complete API suite against PostgreSQL in UTC and America/Chicago, Ruff,
Web type checks, production build and Compose validation. A verified release is
published only after all required CI jobs pass. The manifest contains full Git
commit, CI run, immutable API/worker/Web digests, input-tree hashes and Compose hash.
Unchanged build inputs reuse the previous verified digest.

The workflow checks package visibility before and after publication and refuses an existing public target. Keep GHCR packages private. Actions authenticates with its `GITHUB_TOKEN` and
`packages: write`; NAS Docker must be logged in with a read-only package token.
Do not put registry tokens in Compose, arguments, logs or this document.

Review database timezone and legacy keys before migration, while the previous
workers are still processing. A complete source audit is preparation, not part
of the worker drain window:

```sh
# Run inside the trusted API environment. This explicit wrapper also works
# with the original PR #5 image, before the CLI defaults were improved.
/app/.venv/bin/python - <<'PY'
import json
from sqlalchemy import text
from app.message_key_audit import audit
from app.platform_store import platform_engine
with platform_engine('wechat').connect() as source:
    source.execute(text('SET TRANSACTION READ ONLY'))
    source.execute(text('SET LOCAL cursor_tuple_fraction=1.0'))
    source.execute(text("SET LOCAL work_mem='64MB'"))
    source.execute(text("SET LOCAL statement_timeout='15min'"))
    report = audit(source)
    report.pop('examples', None)
    print(json.dumps(report))
PY
```

The audit reports noncanonical keys and ambiguous candidates. It never changes
primary keys. Ambiguities need source evidence before repair. Streaming uses a
server-side cursor; diagnose it with `EXPLAIN DECLARE ... CURSOR FOR ...`, since
plain `EXPLAIN SELECT` can choose a different plan. The local settings above
optimize a full scan and bound sort memory without changing global parameters.
Updated audit CLIs apply these defaults themselves. Keep the report with the
exact release receipt, then drain workers only when all other preflights pass.

## NAS release

The observed infra checkout is
`/path/to/nas/home/docker/monica-next/infra`.
Install the versioned infra files for the manifest's exact commit, preserving the
production `.env`, routing file and state/history. Set `STORY_SYNC_SECONDS=28800`
when carrying forward the observed NAS cadence. Review memory/pool settings
against representative workload before tightening limits.

Download `release.json` from the private `verified-<full SHA>` draft GitHub release.
Run from that versioned infra directory with Docker privileges:

```sh
python3 deploy-release.py /private/path/release.json
```

The entry point locks deployment, verifies all image references and Compose,
reuses an exact locally recorded `RepoDigest` or pulls a missing fixed digest,
runs migrations under a separate database lock, probes
candidates, drains changed background services, checks their image/start state,
then reloads and verifies Caddy. It records background versions and command timings.
On the NAS, cached-image create/start/exec operations can still take minutes.
Inspect the exact container and journal before retrying a client timeout; do not
start a second installer. Candidate readiness uses a 600-second elapsed startup budget, rather than a fixed number of connection refusals, to tolerate cold imports under high I/O wait. An unhealthy candidate still fails without changing routes. The manifest's transfer time is separate from container
startup, Python probe startup, graceful drain and observation. Preserve these
stage timings instead of presenting cached pulls as an end-to-end benchmark.
The legacy queue bridge requires a `RELEASED` acknowledgement after its database
commit; delayed Docker exec client exit is not a failed business handoff. Rollback
stops every replacement lane before directly starting an unchanged original
container. Already recovered processes are recognized by their new `StartedAt`.
Unchanged services are retained only when image and configuration match. A changed
Web-to-API dependency also requires a new Web container. When API/Web are reused,
retain the previous container pair and both previous Caddy/runtime configuration
paths so a host-only release does not overwrite the working rollback target.

The committed version is observed for five minutes before old API/Web containers
are stopped. Images/configuration remain available. Cleanup failure records an
error without rolling back a healthy committed version. Stale-candidate cleanup
skips running containers and removes stopped containers without `--force`; a
concurrent manual task therefore cannot be killed by a check/remove race. Deferred
cleanup entries survive a later unchanged release. A `.deploy-journal.json`
with restricted permissions records enough previous state to recover interruption;
rerun the same entry point to recover or resume observation. Do not delete that
journal to silence a failure. Background rollback restores code/configuration;
it does not undo committed business writes. If a drain fails before replacement,
recovery retains that original process and continues restoring other roles.
Because it may already have a TERM stop flag, those remaining roles stay in the
journal and recovery reports incomplete until they exit and can be restarted.
For pre-runtime-status source importers, the transition targets the exact Python
module with SIGINT inside its own container, unwinding database sessions and
preserving committed checkpoints; it does not rely on an old uv PID 1 forwarding
TERM. Disable that legacy container's automatic restart for the interrupt,
restore its recorded policy on rollback, and let Compose restore the policy on
replacement. LinkedIn/archive/Stories switch before OCR so a legacy full-cycle disk lock
cannot strand OCR shutdown. Native processes continue using graceful TERM. LinkedIn checks shutdown before each asset, again after disk permission is acquired, and between profile/post units. Before exiting, it flushes pending cross-store post links and commits completed units; an interrupted import leaves its source cursor unchanged for replay. Metadata-loop checks alone do not bound shutdown during media archival.

The lease bootstrap fills physical subject/lane metadata only for active jobs and
recent failed AI jobs used by retry exclusion. Historical terminal payloads are
not rewritten. Their lane is derived by `job_resource(job_type)` for status reads;
reactivating a historical row triggers identity/lane population before claim.
This avoids a full historical payload rewrite under the migration lock. Migration 071
also provides this contract to databases that already applied the original 067.
CRM-only task result transactions defer the job-row lock until final ownership
verification/acknowledgement, so the independent lease heartbeat can renew during
slow queries. Cross-store media materialization retains its early ownership fence. A heartbeat
lock timeout observes only an unexpired matching committed token; it does not
renew through that read or prolong a stalled result indefinitely. Commit-hook
failures roll back the DBAPI transaction before returning a connection to the
pool, or invalidate it if rollback fails.

A migration file prefixed `-- monica: autocommit` contains one idempotent statement
for operations such as `CREATE INDEX CONCURRENTLY`. Ordinary files run in separate
transactions. An interrupted concurrent index build may leave an invalid index;
inspect and repair it before retrying instead of treating `IF NOT EXISTS` as proof
of a usable index. Long data backfills belong in resumable tasks.

The existing `load-action-images.py` and `deploy-staged-images.py` remain explicit
legacy tar fallback tools. Normal deployment uses the manifest entry point; do not
substitute the legacy API-only switch for a coordinated worker/importer rollout.

## Collector compatibility

Deploy the compatible face API/migration first, then the updated Mac face client.
Only recognizable error labels are marked for retry; successful scans, including
zero-face results, remain complete.

Publishers now depend on `infra/snapshot_publish.py`. Install that helper alongside
standalone publisher scripts (or preserve the repository directory layout).
Deploy readers supporting `CURRENT` before enabling the new publishers. Each
publisher creates a private immutable snapshot, validates the transferred manifest,
then replaces `CURRENT`; each importer pins one snapshot for its whole pass.
Existing live-directory input remains readable until first snapshot publication.
Retain old snapshots until consumers and rollback requirements have been checked.
No collection frequency, login policy or quota is changed automatically.

## Backup and isolated restore

Inventory external Synology backups before choosing a new schedule. Use existing
coverage where it meets the full contract. The supplied backup script captures
CRM, all three platform DBs, raw snapshots, staging/checkpoints, exports, media and
versioned deployment configuration. For a coherent set, drain writers or use an
externally coordinated storage snapshot. Separate `pg_dump` commands do not
produce an atomic cross-database snapshot while imports are writing.

```sh
# Supply DATA_ROOT, COMPOSE_FILE and the installed Docker/Compose binary paths.
sh infra/backup.sh
# Always restores into a new disposable container, with no network or host mount.
bash infra/restore-rehearsal.sh /private/backup/set
```

A media snapshot reference alone is not successful media restoration. The restore
checker deliberately refuses complete verification without restored media bytes.
Keep verification output with the backup manifest. Never point a rehearsal at
production databases or active media directories.

## Performance acceptance

Provide a session via `MONICA_COOKIE` in the environment, then run
`infra/benchmark-pages.py` with the same real page API paths, concurrency, warmup
and request count before/after deployment. Run the same importer/model/media load
in both cases. Capture p95/p99, errors, queue wait/throughput, per-role working set,
`/proc/vmstat` swap counters and deployment timings over the same window. Do not
substitute `/ready`, a single request, or synthetic fixtures for business-page
acceptance. Keep account-lock scoping disabled until its concurrency gate passes.

Registry contract reference: [GitHub package visibility and access](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility).
