# WeChat incremental media import

The scheduled `archive-sync` service does not enumerate or re-register the full
Chatlog media cache.

`prefetch-sns-images.py` appends one JSONL record to
`/data/sns-cache/media-import-queue.jsonl` after a downloaded file is present on disk. It also repairs the crash gap
where a Moments file is already cached but its source reference was never
published to the queue. Reused images on new posts are published separately. The importer consumes records after its persisted byte offset,
links only the named files and source records, commits them, and then atomically
advances `/data/staging/continuous-snapshots/media-import-queue-state.json`.
Crashes replay the uncommitted queue suffix; all media writes remain idempotent.

The downloader is bundled in the verified API image (`/app/wechat/`), so a
release updates its policy along with the consumer. The source watcher checks
for changes every five minutes; an empty message shard retries independently
without repeatedly scanning all SNS/contact media. A changed SNS/contact
signature (including WAL) triggers download immediately in that cycle, while
unchanged sources retry media hourly. Existing queue records are linked before
starting downloads and again afterwards.

SNS IDs are signed SQLite int64 values. The consumer accepts negative IDs and
automatically replays records quarantined by the former unsigned-only check.
Recovery retains `.replayed` audit records and advances the existing byte
checkpoint only after media links commit. It never rewinds a good checkpoint.
Transient download backoff starts at one minute and is capped at one hour;
legacy month-long postponements become eligible immediately on upgrade.

Cached bytes and registered content-addressed assets remain separate: a cache
hit must still publish every Moments source reference, and the consumer stages
file reads/copies and cold imports outside its short PostgreSQL transactions.

## Manual full registration

Use a full scan only for audits, legacy caches, or recovery after importing
files outside the normal downloader:

```sh
docker-compose run --rm --no-deps archive-sync \
  /app/.venv/bin/python -u -m app.archive_media \
  --sns-db /data/chatlog/sns/sns.db \
  --cache /data/sns-cache \
  --media-root /data/media
```

The manual command does not alter the incremental queue checkpoint.

## Native video proxy reliability

The NAS Chatlog proxy previously used a 30-second deadline for an entire CDN
response. Large videos failed repeatedly even when Moments text was current.
`chatlog-media-reliability.patch` and the Go files in `chatlog/` target the NAS
source snapshot based on `teest114514/chatlog_alpha` commit
`f3935b0cce3e6cf4e4b1fcd1409e38a9c2e089a7` plus its existing NAS compatibility
changes. Apply the patch in that source tree and copy the two Go files to
`internal/chatlog/http/`, then run that package's tests and the existing
`scripts/build-linux-amd64.sh`.

The fix verifies and joins CDN byte ranges, including servers returning a
partial body with HTTP 200, caps an object at 512 MiB and the complete operation
at 15 minutes, and gives individual requests five minutes. It checks the
128 KiB encrypted video header before attempting an image-sized keystream,
then atomically replaces the cache file only after the complete object is
validated. SNS proxy requests are excluded from the generic access logger so
signed URLs and media keys are not written there. Python video requests allow
the same 15-minute operation budget; photo requests keep their 90-second limit.

`go test infra/wechat/chatlog/sns_download.go infra/wechat/chatlog/sns_download_test.go`
runs standalone in CI and exercises partial HTTP 200, range consistency,
complete/empty bodies, cancellation, and atomic cache replacement. A native
Chatlog binary update is separate from Monica's image rollout. Validate it on
an isolated port with auto-decrypt disabled, test a large video through the
candidate, then keep the previous binary for rollback when switching the live
service. Never run a second full decryption pass to update the HTTP proxy.

## Database watcher startup

Apply `chatlog-database-watch.patch` to the same native source snapshot before
building. The auto-decrypt monitor scans only `db_storage` when the configured
account root contains it. The original configured root remains unchanged for
key lookup and decrypted output paths, so output stays under
`work/db_storage`. Legacy layouts without that subdirectory retain their
existing monitor root. This avoids traversing years of media attachments before
the HTTP listener can start. The media root remains available to HTTP requests.
