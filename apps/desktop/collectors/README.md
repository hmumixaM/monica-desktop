# Bundled collectors

The desktop supervisor invokes the bundled Python interpreter with:

```text
collector.py --provider instagram --data-root <private-app-data> --username <account> login
collector.py --provider linkedin --data-root <private-app-data> login
collector.py --provider <instagram|linkedin> --data-root <private-app-data> <discover|once|watch|status>
```

`login` opens bundled Chromium. The user completes the provider's own password,
2FA, and checkpoint screens. Authentication is verified before a session is
saved. Credentials are never passed on the command line. Run only one action
per provider; stop its watcher before interactive login. A busy action exits
75, a failed single action exits 1, and successful actions emit JSON on stdout.

Writable files live only in `<private-app-data>/collectors/<provider>` with a
private umask. `status.json` contains `state`, `phase`, `updated_at`,
`last_success_at`, `last_success`, `next_run_at`, and an error class without
private diagnostics. `last_success` is an ISO timestamp for the native UI;
the remaining timestamps are Unix seconds. Detailed output stays in
`logs/collector.log`. Cookies, browser state, archives, and query logs are never
part of the distributable.

Instagram discovers the authenticated account's followees, refreshes that
complete list every two weeks, and collects stories, highlights, and profile
history. It keeps the source collector's per-request delays, limited batches,
checkpoint files, and cooldowns. Completed profile histories become eligible
for refresh after two weeks. LinkedIn discovers connections once per day,
collects up to three accounts per two-hour window and 36 per rolling day, and
exports collected profiles with `include_unlinked=True`. Neither collector
needs old Monica contact IDs. The desktop backend creates a distinct profile
for each newly collected account; matching names do not merge people.

Watch intervals are three hours for Instagram and two hours for LinkedIn.
Touching `<private-app-data>/collectors/<provider>/wake` ends an idle wait within
one second, or schedules one follow-up when the current pass finishes.
Cooldowns and rolling budgets still apply. SIGTERM stops the active child
process group and retains completed checkpoints. Login does not erase a
provider checkpoint or cooldown marker.

Required runtime dependencies are pinned in `pyproject.toml` and `uv.lock`.
Set `PLAYWRIGHT_BROWSERS_PATH` to the bundled browser directory. Instagram
children receive `MONICA_INSTAGRAM_ROOT` from the adapter. Optional LinkedIn
manual targets use `MONICA_API_URL=http://127.0.0.1:<port>` plus
`LINKEDIN_COLLECTION_TOKEN`; redirects and non-loopback target feeds are
rejected. The source publisher is found at `runtime/infra/snapshot_publish.py`,
or explicitly through `MONICA_SNAPSHOT_PUBLISHER`.

Publication uses the existing immutable `.snapshots`, atomic `CURRENT`, and
`.crm-ready` protocol locally. Only complete story/highlight media manifests
are published. LinkedIn asset hashes are checked before publishing an export.
Each Instagram phase inventories only the tree it writes, including completed
artifacts when that phase fails. A complete pass performs three inventories
instead of twelve. LinkedIn verifies each distinct content file once and caches
its device, inode, size, mtime and ctime after a successful checksum. Unchanged
files need no byte reads on the next pass; any signature change forces a fresh
SHA-256 check. Source modification during verification aborts publication.
Losing `CURRENT` forces snapshot reconstruction even when source fingerprints
have not changed. Failed publication never advances the published cursor.
Importer roots are:

| Collector output | Importer input |
| --- | --- |
| Instagram profiles/posts | `staging/instagram-current` |
| Instagram stories | `staging/instagram-stories` |
| Instagram highlights | `staging/instagram-highlights` |
| LinkedIn export/assets | `raw/linkedin/linkedin_enrichment.incoming.json` (resolved under `CURRENT`) |

`VENDORED_SOURCES.json` records source hashes and the existing collector repairs
incorporated into this bundle. `sha256` retains the original upstream digest;
`bundled_sha256` pins the adapted source actually shipped. The build verifies
all 29 pins, required capability files, exact package/Git versions, nine CLI
entry points, backend imports, and a real launch of the bundled Chromium. The
contract is in `../runtime/capabilities.json`, enforced by
`../scripts/verify_runtime.py` before Tauri builds the app. It uses temporary
data and does not log in to external providers.
Package only source files and the lock manifest;
exclude `.venv`, `__pycache__`, `.pytest_cache`, and tests from the application.
