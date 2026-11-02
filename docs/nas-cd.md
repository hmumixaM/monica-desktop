> Legacy server tooling reference. This public desktop repository runs validation only; it does not publish verified NAS releases or deploy a NAS. Configure any separate server repository explicitly.

# NAS continuous deployment

Merging a PR to `main` authorizes production deployment. CI runs both timezone
suites, verifies Compose, builds changed images and publishes a complete
`verified-<full SHA>` v2 release in this private repository. NAS checks every two
minutes, pins the newest eligible forward main commit and completes deployment
without a Mac, a browser session or an LLM. An in-progress release is never
superseded. PR and manually dispatched builds cannot publish a production bundle.

## One-time setup

Use a clean, reviewed checkout after all CI checks pass. Finish any existing
manual deployment/recovery first; bootstrap refuses to overwrite its lock or
journal. On the Mac:

```sh
python3 infra/bootstrap-nas-cd.py
python3 infra/bootstrap-nas-cd.py --provision-token
```

The first command installs the executor **paused**, outside versioned infra,
under `~/docker/monica-next/cd`, and registers two native root tasks in DSM Task Scheduler.
It updates only the named Monica CD tasks and archives their prior settings. Both tasks run every
two minutes; cleanup is eligible once daily during 03:30–03:59 America/Chicago,
regardless of the NAS host timezone. A busy deployment causes cleanup to skip.
The tasks persist through normal DSM scheduler regeneration. No self-hosted
Actions runner is installed.

Create a fine-grained GitHub token restricted to `hmumixaM/monica-next-crm`:
Contents **read**, Actions **read**, Deployments **read/write**, implicit Metadata
read. Set a bounded expiry and record its rotation date. Paste it into the hidden
terminal prompt of the second command, never into a chat, a shell command or a
repository file. The setup sends it only over existing SSH and stores it root-only
as `cd/github-token`. Existing GHCR authentication stays separate, read-only and
expires 2027-01-14. `NAS_PASSWORD` is consumed from the environment or a hidden
prompt; bootstrap never saves it. GitHub reauthentication/token creation may need
interactive user completion.

Run `check` to review the eligible version, then `resume` to enable deployment.
The commands run as root via the existing secure NAS SSH/sudo method. The CLI is:

```text
python3 /path/to/nas/home/docker/monica-next/cd/current/nas-cd.py \
  --config /path/to/nas/home/docker/monica-next/cd/config.json \
  check|run-once|status --json|pause|resume|retry FULL_SHA|cleanup
```

The pipe-separated command notation above describes alternatives, not shell
pipelines. `status --json` also works before the credential is installed.
`pause` prevents new deployments immediately while allowing the active one to
finish or recover. `resume` clears the pause; a failed SHA still requires explicit
`retry FULL_SHA`, so resume cannot create a failure loop. Retry keeps the requested
SHA fixed even if later commits are available. A new manual journal is never
silently taken over. An interrupted automatic attempt resumes its recorded
installation before any later release.

## What is verified

The executor requires a complete v2 manifest, this repository's successful
`push/main` CI run from `.github/workflows/ci.yml`, forward main ancestry, fixed
private GHCR digests, archive SHA-256, every regular file hash, Compose hash and
exact migration hashes. Archive traversal, symlinks, protected dotfiles and all
Caddyfiles are rejected. Published release bytes are never replaced by a rerun.
Existing v1 manifests remain manually deployable.

The canonical infra `.deploy.lock` covers installation, pulls, a full read-only
message-key audit before worker drain, migration, candidate checks, coordinated
background replacement, routing and 300-second observation. A fresh audit uses
verified target code if the current image's audit inputs differ. Audit receipts
contain provenance and counts, not message examples. Migration containers are
skipped only with an existing matching migration-hash receipt, the same verified
API application image, and every ledger filename. A changed registry index from
new attestations qualifies only when both immutable references resolve locally
to the same actual image ID; missing image evidence falls back to the existing
locked migrator. Applied migrations are
never automatically downgraded or database backups automatically restored.

After observation, verify the actual images, effective Caddy upstreams, Web's
internal API, all application containers, five lane/scheduler heartbeats and job
leases. Only then publish GitHub Deployment success. The previous API/Web are
retired with rollback configs/images retained. Historical stopped candidates
are deleted in maintenance, using non-forced removal and preserving current and
immediate rollback pairs. Cleanup failure does not roll back healthy services.

## Status and failure handling

Use GitHub's **production-nas** Deployment and one `status --json` call. The status
links to its exact CI run; full sanitized evidence and phase logs are root-only
in `cd/attempts`, with the versioned manifest under `cd/releases/<SHA>` and the
infra backup under `infra/.release-history`. No public endpoint exposes NAS logs.
Routine JSON status retains stage durations and runtime proof but omits the full
per-command timing list; `attempt_record` points to the durable detailed record.
Do not generate per-PR `/tmp` installers, repeatedly poll CI from an agent, invoke
real AI jobs for routine CD or bulk-refresh summaries to verify a release.

Transient GitHub transport failures retry after 2, 5 and then 15 minutes. GitHub
reporting failures are queued durably and retried without redeployment. An
interrupted installation recovers from its pinned local journal even when GitHub
is unreachable; pending reporting cannot block worker/routing recovery. Lock
contention is waiting. Unresolved lifecycle timeouts inspect exact containers
and stop rather than issuing the operation twice. Migration, drain, readiness or
runtime validation failures invoke coordinated recovery and pause further
releases. Recovery failure retains its journal and backup; investigate before
`retry`. The v2 runner never treats Docker Running alone as readiness.

This release retains shared API/importer/OCR and worker images and both timezone
suites. Backend changes may still replace several roles. Use recorded phase
seconds to distinguish CI, audit, disk/lifecycle costs and observation before
proposing image separation or hardware changes. Routine CD consumes no LLM tokens;
troubleshooting still can.

## Disable or remove

Run `pause` first and let an active deployment/recovery finish. Disable only the two native DSM tasks named `Monica CD deploy` and
`Monica CD cleanup` to stop scheduling. Keep the
versioned executor, credential, attempt records and infra history for inspection
or recovery. The task IDs are recorded in `cd/scheduler-tasks.json`; preserve unrelated tasks.
