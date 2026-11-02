# Importer contract

The existing WeChat, Instagram and LinkedIn collectors can emit the same batch
shape and submit it to `POST /api/v1/imports/batches`. An importer should keep
its own source cursor and send the last successful cursor as `cursor_before`.

Every record needs a stable `external_id` and a JSON-serializable `content`.
The API hashes canonical JSON and writes only a new content object when the
hash is new. Repeated records create a lightweight `source_observations` row
with `state=unchanged` and do not create a field revision or a media copy.

For posts and stories, add `entity_type: "post"` or `entity_type: "story"`,
the source `profile_id` once identity matching is confirmed, and
`occurred_at`. For a message stream, add `entity_type: "message"`,
`conversation_type`, `conversation_external_id`, `sender_profile_id`,
`message_type`, and `occurred_at`. These normalized fields let the API write
the correct projection while preserving the complete provider payload in the
raw manifest.

The collector can submit a prepared batch without importing an HTTP client:

```sh
python importers/submit_batch.py /path/to/batch.json \
  --api https://crm.example.ts.net \
  --secret "$WECHAT_IMPORT_SECRET" \
  --token "$WECHAT_IMPORT_TOKEN"
```

The token is optional while a source has no database-managed token. Once a
token is created in Import Center/API, every request for that source must send
the token; revoking it immediately blocks that collector without changing the
other sources.

The existing local source project under
`/path/to/local/cache/monica-wechat-import/` remains the migration source of
truth. Its `monica_payload.json`, Chatlog SQLite files, Instagram matching
inputs, LinkedIn snapshot/enrichment outputs and Monica export scripts should
be converted into batches in this order: Monica/manual baseline, WeChat
profiles/groups/messages/Moments, Instagram profiles/posts/Story coverage,
then LinkedIn profiles/work/education/posts. Matching decisions should be
submitted as identity candidates and accepted manually before projecting a
provider identity onto a person Profile.

## Faces

`importers/faces` is not a batch importer. It runs on the always-on home Mac,
reads avatars and social-post photos through the signed
`/api/v1/faces/device/*` endpoints (source `faces`, with a `faces` collector
token), detects and clusters faces locally, and uploads only face boxes and
suggestions. Embeddings stay in its local SQLite cache. Setup, the launchd job
and the matching rules are in `importers/faces/README.md`.
