# Chatlog Alpha desktop adapter

This directory pins Chatlog Alpha source and carries the small native-service
integration needed by Monica Desktop. It contains no account configuration,
keys, decrypted databases, or compiled executable. Ship `LICENSE` with the app.

## Rebuild

Use Apple Silicon macOS, Go 1.24 or newer, and Xcode command-line tools:

```sh
apps/desktop/patches/chatlog/build.sh /absolute/output/chatlog-alpha
```

`CHATLOG_SOURCE_REPO=/path/to/an/existing/chatlog-alpha/clone` optionally avoids
downloading Git source. The script clones into a temporary directory, checks out
the public upstream commit in `source.json`, applies `desktop.patch`, and builds with CGO.
The existing clone is not changed. The built-in WCDB implementation uses Go and
go-sqlite3; it does not require an external WCDB dylib.

The source pin uses the public `waawei/chatlog_alpha` repository. The patch also
includes the contact metadata API changes used by the desktop importer, so a
build needs no private GitHub repository access.

## Process contract

```sh
chatlog-alpha serve --config-dir /absolute/private/configuration
```

The private directory contains `chatlog-server.json` (write with mode `0600`):

```json
{
  "data_dir": "/absolute/WeChat/account",
  "work_dir": "/absolute/Monica/application-support/chatlog",
  "data_key": "<64-hex-key>",
  "img_key": "<image-key>",
  "platform": "darwin",
  "version": 4,
  "full_version": "4.1.15",
  "http_addr": "127.0.0.1:5030",
  "wal_enabled": true,
  "auto_decrypt": true,
  "auto_decrypt_debounce": 1000
}
```

`data_dir` accepts the account directory or its `db_storage` directory. Work and
cache directories must be separate from the original source. The desktop runtime
allocates the loopback port before starting this process. Only numeric loopback
addresses are accepted; do not expose this private data API on the LAN.

Existing Chatlog master keys and raw per-database keys are both supported: the
adapter checks the first-page HMAC and derives the SQLCipher key when necessary.
An optional `CHATLOG_ALL_KEYS_FILE` points to an existing per-database key map.
The adapter performs no privileged extraction and does not restart WeChat. A
directory alone is insufficient when no valid existing key is available.

`CHATLOG_WCDB_CACHE_DIR` overrides the native cache location; the default is
`work_dir/wcdb-cache`, not the global `~/.chatlog`. Cache/output directories are
private, and no key is logged. Desktop mode disables Chatlog's independent AI,
semantic indexing, graph engine, message hooks, automatic image-key extraction,
and decrypted-media writes into the source. Only read APIs are exposed.

## Readiness and snapshots

- `GET /api/v1/ready` returns `{"ready":true,"mode":"desktop"}` only after real
  contact and session queries succeed. Startup errors exit nonzero.
- `GET /api/v1/desktop/status` returns `snapshots_ready`, `snapshot_files`,
  `updated_last_pass`, `failed_files`, `last_pass_at`, and `watcher`.
- Live endpoints include `/api/v1/contacts`, `/sessions`, `/history`, `/sns_feed`,
  `/sns_search`, and media routes. Append `format=json` to API queries; upstream
  defaults several endpoints to text.
- Historical export runs asynchronously after API startup, writing standalone
  SQLite files to `work_dir/db_storage/{contact,session,sns,message}`. Wait for
  `snapshots_ready` before the first Monica import. A typical existing 1.57 GB
  source completed the initial 13-file export in roughly half a minute.
- Source DB/WAL events trigger a debounced refresh. A 30-second reconciliation
  also handles missed events and wake from sleep. Each destination is atomically
  replaced only after SQLite backup, WAL-mode removal, and `quick_check` succeed.
  Failed exports retain the previous snapshot. WAL parsing includes only the
  last checksum-valid committed transaction, excluding incomplete writer tails.
- SIGTERM/SIGINT cancels the exporter and file monitor, shuts down HTTP, and exits
  without a TUI. A per-configuration process lock prevents duplicate servers.

## Validation

The adapter tests cover loopback/source-path validation, configuration errors,
existing master/raw keys, malformed source retention, committed/uncommitted WAL
frames in both checksum byte orders, read-only route restrictions, and a real
SQLite WAL update that refreshes the exported file and stops on cancellation.

```sh
go test ./internal/wechatdb/wcdbapi ./internal/chatlog ./internal/chatlog/http ./internal/chatlog/database ./cmd/chatlog -count=1
go test -race ./internal/chatlog ./internal/wechatdb/wcdbapi -count=1
```

Local WeChat 4.1.15 validation used a private copy of the existing configuration,
returned 11,582 contacts and real session/history results, exported all 13 selected
databases, and stopped cleanly on SIGTERM. No key extraction was performed. The
watcher update behavior is covered by the isolated SQLite fixture; natural new
WeChat messages were not generated for validation.
