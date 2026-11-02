# API contract for the UI redesign

Backend and `apps/web/lib/` belong to the data layer; `components/`, `app/` and
styles belong to the UI. Read everything through `lib/queries.ts` and
`lib/directory.ts`; TypeScript shapes are in `lib/api-types.ts`.

Mount the provider once in `app/layout.tsx`:

```tsx
import { QueryProvider } from "@/lib/queries";
// <QueryProvider>{children}</QueryProvider>
```

## Conventions

- **Omitted, not empty.** Aggregate endpoints drop a block entirely when it has
  no data: no `0`, `[]`, `{}` or `null` placeholders. Render a section only if
  its key exists.
- **Caching.** Aggregates are cached server-side by data version and return a
  weak `ETag`; revalidation costs a 304. react-query defaults: `staleTime` 60 s,
  `gcTime` 10 min, no refetch on focus.
- **Media.** `localMediaUrl(id)` is always the original file; there are no
  resized variants. Responses are immutable. Images carry `width`/`height`
  once the worker has read them, for layout reservation.
- **Times** are ISO-8601 strings; month keys are `YYYY-MM` in the CRM timezone
  (`America/Chicago`).

## `GET /api/v1/profiles/{id}/overview` — `useProfileOverview(id)`

Replaces the first-paint request fan-out. Keys (each optional except `header`):

| key | content |
| --- | --- |
| `header` | `id`, `type`, `name`, `summary`, `note` (WeChat remark), `company`, `title`, `headline`, `city`, `birthday`, `avatar_media_id`, `avatars[{provider, media_id}]`, `sources[]` |
| `messages` | `months[24]{month, sent, received}` (dense, oldest first, ends this month), `total`, `sent`, `received`, `first_contact_at`, `last_contact_at` |
| `contact_events` | `calls`, `money` (same shapes as `/contact-events/summary`) |
| `relationships` | top 8 `{profile_id, name, relationship_type, direction, avatar_media_id}` |
| `groups` | top 8 groups by recent activity `{profile_id, name, avatar_media_id, role, message_count, last_message_at}` |
| `tags` | `{id, name}` |
| `linkedin_experience` | employment/education facts: `type`, `title`, `company`, `school`, `degree`, `field_of_study`, `location`, `start_date`, `end_date`, `is_current`, `description` |
| `post_stats` | per provider `{posts, last_posted_at}` |
| `places` | top 5 `{label, latitude, longitude, count}` |
| `recent_posts` | 9 `{id, provider, occurred_at, text (≤120 chars), thumbnail_media_id, width, height, media_count}` |

Tab pages keep their detailed endpoints through
`useProfileTab(id, tab, params?)` with `tab` one of `history`,
`structured-history`, `metrics`, `locations`, `relationships`, `photos`,
`chat-months`, `contact-events-summary`, `contact-events`, `moments-circle`,
`messages`, `timeline-summary`, `social`.

### Hand-kept details and uploads

- `PUT /api/v1/profiles/{id}/details` `{field_key, value, replaces: [{field_key, item?}]}`:
  writes `value` as a manual revision of `field_key` and, in the same
  transaction, retires every `replaces` copy (a whole field gets a null
  `manual_delete` revision; `item` removes one entry from a bundled object such
  as `monica.custom_fields`). Imported provider fields, bundled objects and
  `display_name` / `summary` are rejected.
- `DELETE /api/v1/profiles/{id}/details` `{refs: [{field_key, item?}]}` retires
  the listed copies the same way.
- `POST /api/v1/media` accepts any file. Photos and videos must decode as the
  type their bytes claim; other files keep `file_name` and a type guessed from
  the name, and `GET /api/v1/media/{id}` serves them as an attachment (PDF
  inline). Attachment lists carry `file_name` and `byte_length`.
- `/api/v1/profiles/{id}/photos` links photos as `manual_photo` and other files
  as `manual_file`; timeline media lists only photos and videos.

## `GET /api/v1/home` — `useHome()`

- `key_numbers`: `contacts`, `groups`, `messages_30d`, `active_contacts_30d`

## `GET /api/v1/insights?dormant_days=90|180|365` — `useInsights(days)`

View-only attention lists for the 洞察 page; cached 15 minutes and served
stale while imports run.

- `recent_contacts`: 30 most recently contacted `{profile_id, name, avatar_media_id, last_contact_at, direct_message_count, months[12]}`
- `dormant_contacts`: up to 60 people ranked by messages + 10 × calls whose last contact is older than `dormant_days`
- `upcoming_dates`: birthdays, anniversaries and other yearly dates in the next 60 days `{profile_id, name, avatar_media_id, kind, label?, date, days_until, years?}` (`years` is the age or anniversary count, exact dates only)
- `recent_dates`: past 7 days, same shape with `days_ago`
- `recent_posters`: up to 60 people who posted in the last 14 days `{profile_id, name, avatar_media_id, posts, stories, latest_at, providers, thumbnail_media_ids[≤3]}`
- `top_companies`, `top_schools`: top 10 `{id, name, people}`, counted by distinct current people

## Significant dates

`GET /api/v1/profiles/{id}` returns `significant_dates[]` (birthday first) and
`birthday` (the birthday entry or null). Each date is
`{id, kind: birthday|anniversary|other, label, precision: exact|month_day|approximate, year, month, day, years, years_exact, next, days_until}`.
Write with `POST /api/v1/profiles/{id}/dates`, `PUT …/dates/{date_id}`,
`DELETE …/dates/{date_id}` using `{kind, label?, precision, year?, month?, day?}`.
A person has at most one birthday (`409 birthday_exists`); only a birthday
must not be in the future (`422 future`). UI hooks: `useSaveDate`, `useDeleteDate`.

## Directory — `lib/directory.ts`

`GET /api/v1/directory` is a column-oriented snapshot kept in IndexedDB.
Decoded profile fields: `id`, `type`, `name`, `summary`, `avatarMediaId`,
`tagIds`, `companyIds`, `schoolIds`, `aliases`, `providers`, `headline`,
`city`, `birthdayMd` (`MM-DD`), `lastContactAt`, `directMessageCount`, `groupNames` (WeChat group display names, searched but not listed as aliases),
`memberCount` (groups), `conversationId`.

- `useDirectorySearch(text, filters, sort)`: contacts list; `filters` =
  `{type, tagId, companyId, schoolId, provider}`, `sort` = `relevance` | `name`
  | `last_contact` | `messages` | `birthday`. Matches names, aliases, pinyin and
  initials (`zhangsan`, `zs`) locally.
- `usePersonPicker(text, limit)`: people only, for relationship/record pickers.
- `useTagCounts(type?)`: member counts per tag.
- `usePostSearch(text, enabled)`: post text search; loads its own index only
  when enabled.
- Mutation hooks in `queries.ts` patch the local directory immediately
  (`patchDirectoryProfile`); call `clearClientData(queryClient)` on logout.

## Feeds

- `useTimeline({profileId, provider, media: "ready", limit})`: infinite query.
  Each media item has `ready` (bytes stored locally). `media=ready` returns only
  events with local media and only their ready items. Placeholder texts
  (story coverage states, provider names) come back as `summary: null`.
- `useConversations(q)`: most recent first; rows carry `last_message_at`,
  `last_message_preview` (≤80 chars, plain text), `message_count`,
  `avatar_media_id`, and `cursor`.
- `useConversationMessages(conversationId, params)`: older pages by
  `before`/`before_id`.
- `useMessageSearch(q)`: server-side message and activity search.

## `GET /api/v1/profiles/{id}/group-speech`

Live read of one person's remarks in their current WeChat groups. The button
on a person profile calls this directly; it is not a cached aggregate and it
does not write messages into Monica. Optional `group_id`, `q`, `since`,
`until`, and `limit` (default 50, max 200). The response is
`{ senders, groups[{ group_id, name, chat, messages[{ time, sender, text }], error }] }`.
A room Chatlog cannot serve comes back with `error` and an empty `messages` list.

## Visualizations

- `GET /api/v1/profiles/{id}/ribbon` (`useProfileTab(id, "ribbon")`): the
  relationship ribbon. `{ timezone, months[{ month, sent, received, calls,
  interactions }] }` over the whole history; counts only, never message text.
- `GET /api/v1/insights/orbit`: the 5 / 15 / 50 / 150 rings. `people[]` carry
  `community`, `ring` and message counts per window (keys `"3"` and `"12"`, in months), plus
  `calls` and `last_contact_at`. Communities come from Louvain over chats, groups,
  Moments interactions and confirmed photo co-appearances; their names come from
  the model or a manual rename.
- `GET /api/v1/insights/calendar?year=` and `/calendar/day?day=`: who I talked
  to on each day (messages and calls), for the contact calendar.
- `GET /api/v1/relationships/graph`: edges may carry `photos` (confirmed
  co-appearances), which also count as a direct tie.

## Model features — `lib/ai.ts`

Present only when `AI_ENABLED` is set; otherwise the endpoints answer
`{ enabled: false }` and the UI renders nothing for them.

- `useAiStatus()` / `useRunAiJob()`: `GET /api/v1/ai/status`,
  `POST /api/v1/ai/jobs/{ai.community_names|ai.reconnect|ai.digest|ai.profiles|ai.person_weeks}`.
- `useProfileAi(id)` / `useRefreshProfileAi(id)`: `GET /api/v1/ai/profiles/{id}`
  returns `{ enabled, queued, memory: { summary, facts[], gaps[], generated_at }
  | null, weekly_updates[{ week_start, summary, sources[], generated_at }], suggestions[] }`;
  it polls while `queued`. `POST …/refresh` queues a Memory job and the latest
  completed week's personal update (202).
- `useWeeklyHighlights()`: `GET /api/v1/ai/weekly-highlights` returns up to four
  random people with personal notes from the latest completed week.
- A suggestion is `{ fingerprint, kind, label, value, date, quote, confidence:
  "high" | "medium", evidence[{ kind: "direct" | "group" | "post", id,
  conversation_id?, at, speaker?, group?, text }] }`.
- `useSuggestionPage(kind, limit)`: `GET /api/v1/ai/suggestions?kind&offset&limit`
  → `{ enabled, total, items, kinds{kind: count}, people[{ profile_id, name,
  avatar_media_id, generated_at, items[] }] }`; `limit` counts people (≤ 100).
- `useDecideSuggestion()`: `POST /api/v1/ai/suggestions/{profile_id}/{fingerprint}`
  with `{ action: "accept", value?, date? }` or `{ action: "ignore" }`; `DELETE`
  undoes an ignore. Accepting writes a field, a birthday (409 if one exists) or
  an activity, and refreshes the profile.
- `useReconnect()` / `useReconnectAction()`: `GET /api/v1/reconnect` returns
  `{ enabled, people[{ profile_id, name, …, suggestion: { reason, opener,
  sources[] } | null }] }`. `POST /api/v1/reconnect/{id}` with
  `{ state: "done" | "snoozed" | "dismissed", snooze?: "2w" | "1m" }`;
  `DELETE` undoes it.
- `useDigests()` / `useDigest(week)`: `GET /api/v1/digests` and
  `/digests/{week_start}`.
- `useRenameCommunity()`: `PUT` / `DELETE /api/v1/communities/{id}/name`.

## Faces — `lib/faces.ts`

- `useFaceSummary()`: `GET /api/v1/faces/summary` →
  `{ scanned, last_scan_at, faces, to_review, confirmed, people }`.
- `useFaceGroups()`: `GET /api/v1/faces/groups?offset&limit` (infinite). Each
  group is `{ key: "p:<profile>" | "c:<cluster>", size, best, person, faces[] }`;
  groups with a suggested person come first.
- `useDecideFaces()`: `POST /api/v1/faces/decide`
  `{ face_ids, action: "confirm" | "not" | "ignore", profile_id? }` returns the
  `previous` states; `useRestoreFaces()` posts them to `/faces/restore` to undo.
- `useFacesInMedia(ids)`: `GET /api/v1/faces/in-media?ids=` — confirmed or
  suggested people in these photos.
- `useAppearances(profileId)`: `GET /api/v1/faces/people/{id}?offset` →
  `{ total, items[{ id, media_id, box, post_id, occurred_at, provider, author,
  others[] }], next_offset }`.
- Boxes are `[left, top, width, height]` as fractions of the image;
  `FaceCrop` draws them from the original.
- `/api/v1/faces/device/*` is for the Mac worker only: signed requests and a
  `faces` collector token, never the browser session.

### LinkedIn manual collection

- `GET /api/v1/profiles/{id}/linkedin-targets`: browser-authenticated manual
  collection entries linked to a person Profile.
- `PUT /api/v1/profiles/{id}/linkedin-targets`, `{ "account": "URL or slug" }`:
  canonicalizes a personal LinkedIn account, confirms its reversible source link,
  and enables manual collection. Returns `account_id`, `external_id`, and
  `profile_url`. Invalid input/group targets return 422; ownership conflicts
  return 409. Browser mutations require the normal CSRF token.
- `DELETE /api/v1/profiles/{id}/linkedin-targets/{account_id}`: disables only
  manual collection and keeps source content, revisions, and account ownership.
- `GET /api/v1/linkedin/collection-targets`: a version-1 minimal target feed for
  the external collector, authenticated only by its read-only
  `X-LinkedIn-Targets-Token`. No profile names or IDs leave the CRM in this feed.
