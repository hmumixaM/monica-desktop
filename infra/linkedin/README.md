# LinkedIn source collection

Install `profile_media.py` into the existing collector's `src/linkedin_enrichment`
and `publish-crm.sh` into its `scripts` directory, then run
`python3 configure_collector.py /path/to/linkedin-enrichment`.
The installer can be repeated; it stops when an expected upstream code anchor changes.
Keep the collector's existing login, schedule, quota, and authentication circuit settings.

The regular Monica export still contains only accounts with a numeric `contact_id`.
The CRM export uses `export output/linkedin_enrichment.crm.json --include-unlinked`.
It also retains source-native accounts with no Profile link, including their avatars,
posts, and immutable snapshots. Exporting an account never authorizes a Profile link.
`sync-host.sh` atomically publishes this separate file to the NAS after collection.

Validate against a local copy of the installed collector, without its private data:

```sh
python3 infra/linkedin/configure_collector.py /path/to/collector-copy
PYTHONPATH=/path/to/collector-copy/src uv run --project /path/to/collector-copy \
  pytest /path/to/monica/infra/linkedin/test_crm_export.py /path/to/collector-copy/tests/test_storage.py
```

After a release, verify the actual NAS incoming file, account count, media manifests,
and local avatar bytes. A successful collector run alone is not proof of CRM ingestion.

## Local media archive

For the collector with an `assets` SQLite catalog, install `publish-local-media.py`
into its scripts directory and run `configure-local-media.py` against the collector.
The normal collection cycle exports all source accounts, verifies local file hashes,
and publishes deduplicated assets before atomically replacing the NAS manifest.
The NAS importer verifies bytes again and associates avatars, backgrounds and post
media by exact source account/post identifiers. Unreferenced catalog files remain
archived without inventing an owner. No collection frequency or login settings change.
Missing files fail ingestion before advancing the import cursor. Replays preserve
existing links and source history.

## Current activity cards

Install `activity_posts.py` and the scraper/storage adaptations with
`python3 infra/linkedin/configure-activity-posts.py /path/to/linkedin-enrichment`
while that collector is idle. The installer preserves separate
`.before-sdui-posts` backups. Test a private-data-free source copy first with the
collector's environment and its complete test suite.

The SDUI activity layout lacks the old `data-urn` elements. Its post control
menu exposes the outer post's identifier; a URL in the body can identify a quoted
post instead. For share/ugcPost identifiers, the parser captures the menu's copy
URL in a temporary page-local clipboard sink and resolves the LinkedIn short
link to its canonical activity. It never writes to the OS clipboard, or invokes
a share, save, or report action. This preserves deduplication with old activity
IDs. Clipboard methods are restored immediately after URL capture. Visible
content without a readable identifier fails explicitly rather than recording
an empty successful capture. Known/pinned cards do not stop the bounded scan.

On NAS, unchanged local assets use a bulk archive-cache lookup and do not acquire
heavy disk permission or rewrite an unchanged URL mapping. New/modified files
still take the disk permit and pass the content hash check before linking. URL
mapping writes commit in batches of 100; stop checkpoints flush only completed
work and leave the whole-export cursor unchanged.

Run `audit-profile-media.py --snapshot <published-manifest>` after each archive.
It compares every avatar and background URL in the frozen manifest with the local
asset list, source-account media, Profile projection, and on-disk content-addressed
file. Completion requires all four counts to equal the expected count and every
missing list to be empty. Browser acceptance still verifies representative avatar
and background elements use `/api/v1/media/...` without external media requests.
If the collector manifest grows before the ordinary metadata sync catches up, run
`import-missing-posts.py --snapshot <published-manifest>` under the importer lock,
then replay the audit and media archive. It imports only stable post IDs absent from
the CRM and rejects missing or ambiguous source owners.

For a verified manifest whose accounts/posts already exist, archive local bytes
without waiting for historical snapshot replay:

```sh
python -m app.sync_linkedin --source /data/raw/linkedin/linkedin_enrichment.incoming.json --media-only
```

This takes the same importer lock, leaves metadata cursors unchanged, and cannot
be combined with `--watch`. Catalog registration commits in batches of 100;
post links retain bounded source/CRM batches and account links retain individual commits. Resume the normal watcher after
this operation. Catalog files without a trustworthy post ID remain archived
without assigning them to a fabricated post.

## Manually followed LinkedIn accounts

A person Profile's **来源账号 → LinkedIn 持续采集** accepts a personal `/in/`
URL or public identifier. Saving confirms ownership through the existing source
account link, records an audit event, and adds the account to the explicit CRM
collection list. It does not require a connection or a legacy Monica contact ID.
An account owned by a different Profile requires the existing account-management
flow; saving here never moves it silently. Repeating a save is idempotent.

The collector reads `GET /api/v1/linkedin/collection-targets` using
`X-LinkedIn-Targets-Token`. Configure a long random `LINKEDIN_COLLECTION_TOKEN`
in the NAS Compose `.env`; this credential authorizes only reading the minimal
account feed and grants neither browser access nor import/write access. Missing
or incorrect tokens return 401. The feed reads committed database state on every
request, excludes unlinked/archived/quarantined accounts, and includes no Profile
names or IDs. Token rotation must update both NAS and collector configuration.

While the separate Mac collector is idle, install the integration with:

```sh
python3 infra/linkedin/configure-manual-targets.py /path/to/linkedin-enrichment
```

The installer checks known code anchors, preserves `.before-manual-targets`
backups, and is safe to repeat. Put the token in a mode-0600 private file on the
collector, and create its mode-0600 `data/crm-targets.json` containing `url` (the
HTTPS endpoint above) and `token_file` (the absolute token-file path). Secrets
must never be passed in command-line arguments, source control, or logs.

Both `sync-host.sh` and `scan-connections-host.sh` refresh manual membership after
their existing source lists. Canonical identifiers reuse existing queue entries
and snapshot history, including regional and trailing-slash profile URLs. Source
membership reconciliation removes only the list being refreshed: a connection,
a legacy contact, or a manual target independently keeps an account active. A
failed or invalid feed keeps the previous manual queue and reports the failed
refresh while ordinary collection continues. HTTPS verification remains enabled,
redirects are rejected, and responses are bounded to 2 MiB.

**停止手动采集** removes the manual membership; it retains the account link and
all historical data. A connection/legacy list may still collect that account.
The existing schedule, pilot settings, budgets, login, access checks, retry rules,
and circuit breaker remain in effect. Saving queues a future normal collection;
it does not immediately scrape or bypass LinkedIn visibility restrictions.

Validate against a private-data-free copy of the installed collector:

```sh
python3 infra/linkedin/configure-manual-targets.py /path/to/collector-copy
PYTHONPATH=/path/to/collector-copy/src uv run --project /path/to/collector-copy \
  pytest /path/to/collector-copy/tests /path/to/monica/infra/linkedin/test_manual_targets.py
```

Rollback restores the three backed-up collector files and removes the new helper
modules/configuration. Keep the opt-in CRM rows and all source history intact.
