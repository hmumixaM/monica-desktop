from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from .content import get_or_create_content


def utcnow() -> datetime:
    return datetime.now(UTC)


def create_profile(db: Session, profile_type: str, display_name: str, summary: str | None):
    return (
        db.execute(
            text("""
            INSERT INTO profiles(profile_type, display_name, summary)
            VALUES (:profile_type, :display_name, :summary)
            RETURNING id, profile_type, display_name, summary, created_at, archived_at
        """),
            {"profile_type": profile_type, "display_name": display_name, "summary": summary},
        )
        .mappings()
        .one()
    )


def get_profile(db: Session, profile_id: UUID, *, for_update=False):
    return (
        db.execute(
            text("""
            SELECT id, profile_type, display_name, summary, avatar_media_id, created_at, archived_at
            FROM profiles WHERE id = :profile_id AND archived_at IS NULL
        """ + (" FOR KEY SHARE" if for_update else "")),
            {"profile_id": profile_id},
        )
        .mappings()
        .one_or_none()
    )


def list_profiles(db: Session, query: str | None, limit: int, cursor: str | None, profile_type=None, company=None, school=None, tag=None):
    params: dict[str, Any] = {"limit": limit}
    clauses = ["archived_at IS NULL"]
    if query:
        clauses.append("(display_name ILIKE :query OR summary ILIKE :query)")
        params["query"] = f"%{query}%"
    if profile_type in {'person','group'}:
        clauses.append('profile_type=:profile_type'); params['profile_type']=profile_type
    if tag:
        clauses.append('EXISTS (SELECT 1 FROM tag_memberships t WHERE t.profile_id=profiles.id AND t.tag_id=:tag)');params['tag']=tag
    for value, table, entity, fk, key in [(company,'profile_employment_revisions','companies','company_id','company'),(school,'profile_education_revisions','schools','school_id','school')]:
        if value:
            clauses.append(f"EXISTS (SELECT 1 FROM profile_structured_current c JOIN {table} r ON r.id=c.current_revision_id JOIN {entity} e ON e.id=r.{fk} WHERE c.profile_id=profiles.id AND e.name=:{key})")
            params[key]=value
    if cursor:
        clauses.append("id > :cursor")
        params["cursor"] = cursor
    return list(
        db.execute(
            text(f"""
            SELECT id, profile_type, display_name, summary, avatar_media_id, created_at, archived_at
            FROM profiles WHERE {" AND ".join(clauses)}
            ORDER BY id LIMIT :limit
        """),
            params,
        ).mappings()
    )


def append_field_revision(
    db: Session,
    *,
    profile_id: UUID,
    field_key: str,
    value: Any,
    source_type: str,
    operation: str,
    source_record_id: str | None = None,
    actor_user_id: UUID | None = None,
    observed_at: datetime | None = None,
    is_conflict: bool = False,
    source_account_id: UUID | None = None,
) -> tuple[dict[str, Any], bool]:
    if source_type == 'manual' and profile_id and not get_profile(db, profile_id, for_update=True):
        from fastapi import HTTPException
        raise HTTPException(409, 'Profile was archived; refresh before editing')
    observed_at = observed_at or utcnow()
    content_id, digest = get_or_create_content(db, value=value, content_kind="field_value")
    # Serialize transitions for one field, including manual/provider interleaving.
    db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
               {"key": f"field:{profile_id}:{field_key}"})
    current = (
        db.execute(
            text("""
            SELECT current_revision_id, current_content_hash, current_source_type
            FROM profile_field_current
            WHERE profile_id = :profile_id AND field_key = :field_key
        """),
            {"profile_id": profile_id, "field_key": field_key},
        )
        .mappings()
        .one_or_none()
    )
    latest_source = db.execute(text("""
        SELECT id, profile_id, field_key, content_hash, source_type, operation, observed_at, is_conflict
        FROM profile_field_revisions
        WHERE field_key=:field AND source_type=:source AND (
          (CAST(:account AS uuid) IS NOT NULL AND source_account_id=:account) OR
          (CAST(:account AS uuid) IS NULL AND profile_id=:profile AND source_record_id IS NOT DISTINCT FROM :record))
        ORDER BY revision_seq DESC LIMIT 1
    """), {"profile": profile_id, "field": field_key, "source": source_type,
           "record": source_record_id,"account":source_account_id}).mappings().one_or_none()
    if operation == "import" and latest_source and latest_source["content_hash"] == digest:
        return dict(latest_source), False
    if operation != "import" and current and current["current_content_hash"] == digest:
        revision = db.execute(text("SELECT * FROM profile_field_revisions WHERE id=:id"),
                              {"id": current["current_revision_id"]}).mappings().one()
        return dict(revision), False
    is_conflict = bool(is_conflict or (current and current["current_source_type"] in {"manual", "monica"} and source_type not in {"manual", "monica"} and current["current_content_hash"] != digest))
    previous_revision_id = latest_source["id"] if latest_source else (current["current_revision_id"] if current else None)
    revision = (
        db.execute(
            text("""
            INSERT INTO profile_field_revisions
              (profile_id, field_key, content_object_id, content_hash, source_type,
               source_record_id, actor_user_id, observed_at, effective_at, operation, is_conflict,
               previous_revision_id,source_account_id)
            VALUES (:profile_id, :field_key, :content_id, :digest, :source_type,
                    :source_record_id, :actor_user_id, :observed_at, :observed_at, :operation,
                    :is_conflict, :previous_revision_id,:source_account_id)
            RETURNING id, profile_id, field_key, content_hash, source_type, operation,
                      observed_at, is_conflict
        """),
            {
                "profile_id": profile_id,
                "field_key": field_key,
                "content_id": content_id,
                "digest": digest,
                "source_type": source_type,
                "source_record_id": source_record_id,
                "actor_user_id": actor_user_id,
                "observed_at": observed_at,
                "operation": operation,
                "is_conflict": is_conflict,
                "previous_revision_id": previous_revision_id,
                "source_account_id":source_account_id,
            },
        )
        .mappings()
        .one()
    )
    # Manual edits and explicit accept/restore operations become the current projection.
    # Provider changes that collide with a manual value remain history/conflict candidates.
    should_project = (
        not current or source_type == "manual" or (source_type == "monica" and current["current_source_type"] != "manual") or current["current_source_type"] not in {"manual", "monica"}
    )
    if source_account_id:
        db.execute(text('''INSERT INTO source_account_fields(account_id,field_key,revision_id) VALUES(:account,:field,:id)
            ON CONFLICT(account_id,field_key) DO UPDATE SET revision_id=EXCLUDED.revision_id'''),
            {'account':source_account_id,'field':field_key,'id':revision['id']})
    if should_project and profile_id:
        db.execute(
            text("""
                INSERT INTO profile_field_current
                  (profile_id, field_key, current_revision_id, current_content_hash,
                   current_source_type, updated_at,source_account_id)
                VALUES (:profile_id, :field_key, :revision_id, :digest, :source_type, :now,:source_account_id)
                ON CONFLICT (profile_id, field_key) DO UPDATE SET
                  current_revision_id = EXCLUDED.current_revision_id,
                  current_content_hash = EXCLUDED.current_content_hash,
                  current_source_type = EXCLUDED.current_source_type,
                  updated_at = EXCLUDED.updated_at,source_account_id=EXCLUDED.source_account_id
            """),
            {
                "profile_id": profile_id,
                "field_key": field_key,
                "revision_id": revision["id"],
                "digest": digest,
                "source_type": source_type,
                "now": observed_at,
                "source_account_id":source_account_id,
            },
        )
        if field_key in {'display_name', 'summary'} and isinstance(value,str):
            db.execute(text(f'UPDATE profiles SET {field_key}=:value WHERE id=:id'),
                       {'value':value,'id':profile_id})
    return dict(revision), True


def list_field_history(db: Session, profile_id: UUID, field_key: str | None = None, limit: int = 100, before: int | None = None):
    clauses = ["((r.source_account_id IS NULL AND r.profile_id=:profile_id) OR EXISTS (SELECT 1 FROM source_account_links l WHERE l.account_id=r.source_account_id AND l.profile_id=:profile_id))"]
    params: dict[str, Any] = {"profile_id": profile_id}
    if field_key:
        clauses.append("r.field_key = :field_key")
        params["field_key"] = field_key
    if before:
        clauses.append('r.revision_seq < :before')
        params['before'] = before
    params['limit'] = min(max(limit, 1), 200)
    return list(
        db.execute(
            text(f"""
            SELECT r.id, r.profile_id, r.field_key, r.content_hash, r.source_type, r.operation,
                   r.observed_at, r.is_conflict, r.revision_seq, co.payload_json AS value
            FROM profile_field_revisions r
            JOIN content_objects co ON co.id = r.content_object_id
            WHERE {" AND ".join(clauses)}
            ORDER BY revision_seq DESC LIMIT :limit
        """),
            params,
        ).mappings()
    )


def add_observation(
    db: Session,
    *,
    batch_db_id: UUID,
    source: str,
    stream: str,
    external_id: str,
    source_cursor: str | None,
    digest: str,
    state: str,
    observed_at: datetime,
    source_updated_at: datetime | None,
):
    return db.execute(
        text("""
          INSERT INTO source_observations
            (source, stream, external_id, batch_id, source_cursor, content_hash,
             observed_at, state, source_updated_at)
          VALUES (:source, :stream, :external_id, :batch_id, :source_cursor, :digest,
                  :observed_at, :state, :source_updated_at)
          ON CONFLICT (batch_id, source, stream, external_id) DO UPDATE SET
            content_hash = EXCLUDED.content_hash,
            state = EXCLUDED.state,
            observed_at = EXCLUDED.observed_at,
            source_updated_at = EXCLUDED.source_updated_at
          RETURNING id
        """),
        {
            "source": source,
            "stream": stream,
            "external_id": external_id,
            "batch_id": batch_db_id,
            "source_cursor": source_cursor,
            "digest": digest,
            "observed_at": observed_at,
            "state": state,
            "source_updated_at": source_updated_at,
        },
    ).scalar_one()
