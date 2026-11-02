# Daily local Story ingestion

On home, `publish-stories.py` publishes complete `data/stories/<username>/story-<id>.manifest.json`
archives and their listed files through the existing `linkedin-nas` SSH alias. Media is installed atomically before
manifests, with a separate publication checkpoint. This never contacts Instagram or changes collector cadence.

NAS service `instagram-story-sync` reads `/data/staging/instagram-stories`, uses the enabled Instagram source store,
projects `social_stories`, and registers local media. Numeric owner IDs determine source accounts; unknown accounts
remain unlinked and never create people. Expired Stories remain archived. A database lease prevents overlapping runs;
checkpoints advance only after database and media commits. Failed or incomplete items retry next cycle.

The observed Mac publication runs daily (86400 seconds); NAS consumption runs every eight hours
(`STORY_SYNC_SECONDS=28800`). Keep the deployed frequencies unless explicitly changed. The collector's three-hour schedule and
its authentication cooldown remain unchanged. First publication/import and replay are validated manually.

Home manual commands:

```sh
python3 /Users/max/instagram-archive/publish-stories-crm.py --root /Users/max/instagram-archive --dry-run
python3 /Users/max/instagram-archive/publish-stories-crm.py --root /Users/max/instagram-archive
```

NAS manual import using the API image and ordinary database/media/staging mounts:

```sh
/app/.venv/bin/python -m app.sync_instagram_stories
```

No remote image fallback or provider download job is scheduled by this importer.

## Post owner coverage

Run `audit-post-owners.py --root /data/staging/instagram-current/profiles`
after publishing a new post snapshot. It reports exact numeric owners whose collected
posts do not yet have a Profile. With the user's separate-profile policy, rerun with
`--apply` to create and link those people from source account IDs; it never matches
names. A replay must report zero missing owners and zero missing posts.

## Collected post metadata

The archive refresh uses `reconcile_sources.instagram`. Each `*_UTC_*.json` post
is read together with its matching `post-<shortcode>.raw-node.json` and
`post-<shortcode>.relationships.json`, when present. Sidecar shortcode / raw post
identity mismatches fail the read instead of assigning another post's data.
Keep these sidecars when publishing an archive; the main JSON alone can omit
comment/reply identities and collection-policy evidence.

The normalizer preserves the permanent post link, post ID / shortcode / time,
caption, image/video/carousel type, per-item media IDs, source URLs, dimensions,
durations, available alternative resources, tags, mentions, counts, and source
location fields. Location coordinates are provider-returned values; they are
not invented from a place name or treated as device EXIF. Identity previews are
limited to five commenters, five repliers, and five likers. Missing, empty, and
policy-skipped collections remain distinguishable.

A read-only comparison and incremental apply use the same source parser:

```sh
/app/.venv/bin/python -m app.reconcile_sources instagram --root /data/staging/instagram-current/profiles
/app/.venv/bin/python -m app.reconcile_sources instagram --root /data/staging/instagram-current/profiles --apply
```

Source objects are committed before their linked CRM projections. Existing
explicit source-account links determine ownership; names do not create links.
Apply only differing content, then replay against the same fixed archive to
verify zero differences. This command does not download from Instagram.

Cards show the post type, permanent link, tags, mentions, and location. Details
show identifiers, counts, collected identity previews, and expandable carousel
metadata. Source media URLs are user-opened references and may expire. Images
and video previews continue to use local archived media only.

Image refresh jobs use a video's thumbnail URL in a separate poster slot. Video
bytes stay in the local archive; the image worker does not download or periodically
revalidate them as images. This preserves the playable local video when its poster changes.

Publishers require the shared `infra/snapshot_publish.py` helper and now atomically publish an immutable `CURRENT` snapshot. See `docs/reliability/ROLLOUT.md` for the reader-first rollout.
