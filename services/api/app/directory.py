"""Compact contact directory for client-side search, filtering and sorting.

The browser keeps this snapshot in IndexedDB and revalidates it with the ETag,
so contact lists, pickers and tag counts no longer query the server per key.
Tags, companies and schools are dictionary-encoded as indices into small lists.
"""

from collections import defaultdict

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from .aggregate_cache import cached_endpoint
from .auth import current_user
from .data_versions import DIRECTORY
from .db import get_db
from .profile_facts import (
    GROUP_NAMES_FIELD,
    aliases,
    birthday_md,
    city,
    experiences,
    field_texts,
    group_names,
    headline,
)

router = APIRouter()

PROFILE_COLUMNS = ("id", "type", "name", "summary", "avatar_media_id", "tag_ids", "company_ids", "school_ids",
                   "aliases", "providers", "headline", "city", "birthday_md", "last_contact_at",
                   "direct_message_count", "member_count", "conversation_id", "group_names")
POST_COLUMNS = ("id", "profile_id", "provider", "occurred_at", "text")
POST_TEXT_LENGTH = 200


def _dictionary(rows):
    index = {}
    values = []
    for row_id, name in rows:
        index[str(row_id)] = len(values)
        values.append([str(row_id), name])
    return index, values


def build_directory(db) -> dict:
    profiles = db.execute(text("""SELECT p.id, p.profile_type, p.display_name, p.summary,
          COALESCE(p.avatar_media_id, avatar.media_id) AS avatar_media_id,
          b.month, b.day, s.last_contact_at, NULLIF(s.direct_message_count, 0) AS direct_message_count,
          direct.id AS conversation_id
        FROM profiles p
        -- One pass over platform avatars instead of a lookup per profile.
        LEFT JOIN (SELECT DISTINCT ON (l.entity_id) l.entity_id, l.media_id
          FROM media_links l JOIN media_assets m ON m.id = l.media_id
          WHERE l.entity_type = 'profile' AND l.role IN ('wechat_avatar', 'linkedin_avatar', 'instagram_avatar')
          ORDER BY l.entity_id, m.created_at DESC) avatar ON avatar.entity_id = p.id
        LEFT JOIN profile_dates b ON b.profile_id = p.id AND b.kind = 'birthday'
        LEFT JOIN profile_contact_stats s ON s.profile_id = p.id
        LEFT JOIN LATERAL (SELECT c.id FROM conversations c WHERE c.profile_id = p.id
          ORDER BY c.conversation_type = 'direct' DESC, c.last_message_at DESC NULLS LAST LIMIT 1) direct ON true
        WHERE p.archived_at IS NULL
        ORDER BY p.display_name, p.id""")).mappings().all()
    tag_index, tags = _dictionary(db.execute(text("SELECT id, name FROM tags ORDER BY name, id")).all())
    tag_ids = defaultdict(list)
    for tag_id, profile_id in db.execute(text("SELECT tag_id, profile_id FROM tag_memberships")).all():
        tag_ids[str(profile_id)].append(tag_index[str(tag_id)])
    facts = experiences(db)
    company_rows = {}
    school_rows = {}
    for rows in facts.values():
        for fact in rows:
            if fact.get("company_id"):
                company_rows[fact["company_id"]] = fact["company"]
            if fact.get("school_id"):
                school_rows[fact["school_id"]] = fact["school"]
    company_index, companies = _dictionary(sorted(company_rows.items(), key=lambda item: item[1]))
    school_index, schools = _dictionary(sorted(school_rows.items(), key=lambda item: item[1]))
    fields = field_texts(db, active_only=True)
    identity_names = defaultdict(list)
    for profile_id, display_name, username in db.execute(text("""SELECT profile_id, display_name, username
            FROM identities WHERE profile_id IS NOT NULL AND status NOT IN ('invalid','merged')""")).all():
        identity_names[str(profile_id)].extend(value for value in (display_name, username) if value)
    providers = defaultdict(set)
    for profile_id, provider in db.execute(text("""SELECT l.profile_id, a.provider FROM source_account_links l
            JOIN source_accounts a ON a.id = l.account_id
            UNION SELECT profile_id, provider FROM identities
            WHERE profile_id IS NOT NULL AND status NOT IN ('invalid','merged')
              AND provider IN ('wechat','instagram','linkedin','monica')""")).all():
        providers[str(profile_id)].add(provider)
    members = dict(db.execute(text("""SELECT m.group_profile_id, count(*) FROM group_memberships m
        JOIN profiles p ON p.id = m.person_profile_id AND p.profile_type = 'person' AND p.archived_at IS NULL
        WHERE m.left_at IS NULL AND m.person_profile_id <> m.group_profile_id
        GROUP BY m.group_profile_id""")).all())
    rows = []
    for row in profiles:
        pid = str(row["id"])
        profile_facts = facts.get(pid, [])
        profile_fields = fields.get(pid, {})
        rows.append([
            pid, row["profile_type"], row["display_name"], row["summary"],
            str(row["avatar_media_id"]) if row["avatar_media_id"] else None,
            sorted(tag_ids.get(pid, [])),
            sorted({company_index[f["company_id"]] for f in profile_facts if f.get("company_id")}),
            sorted({school_index[f["school_id"]] for f in profile_facts if f.get("school_id")}),
            (names := aliases(row["display_name"], profile_fields, identity_names.get(pid, []))),
            sorted(providers.get(pid, ())),
            headline(profile_fields, profile_facts) if row["profile_type"] == "person" else None,
            city(profile_fields),
            birthday_md(row),
            row["last_contact_at"],
            int(row["direct_message_count"]) if row["direct_message_count"] else None,
            int(members.get(row["id"], 0)) if row["profile_type"] == "group" else None,
            str(row["conversation_id"]) if row["conversation_id"] else None,
            group_names(profile_fields.get(GROUP_NAMES_FIELD), [row["display_name"], *names]),
        ])
    return {"columns": PROFILE_COLUMNS, "profiles": rows, "tags": tags, "companies": companies, "schools": schools}


def build_post_index(db) -> dict:
    rows = db.execute(text("""SELECT p.id, p.profile_id, p.provider, p.occurred_at, left(p.search_body, :length)
        FROM social_posts p WHERE p.search_body IS NOT NULL AND btrim(p.search_body) <> ''
        ORDER BY p.occurred_at DESC NULLS LAST, p.id"""), {"length": POST_TEXT_LENGTH}).all()
    return {"columns": POST_COLUMNS,
            "posts": [[str(r[0]), str(r[1]) if r[1] else None, r[2], r[3], r[4]] for r in rows]}


@router.get("/api/v1/directory")
@cached_endpoint("directory", DIRECTORY, ttl=3600, stale_domains=("messages", "posts", "media", "contact_events"))
def directory_snapshot(db: Session = Depends(get_db), _user=Depends(current_user)):
    return build_directory(db)


@router.get("/api/v1/directory/posts")
@cached_endpoint("directory-posts", ("posts", "profiles", "links"), ttl=3600, stale_domains=("messages", "posts", "media", "contact_events"))
def directory_posts(db: Session = Depends(get_db), _user=Depends(current_user)):
    return build_post_index(db)
