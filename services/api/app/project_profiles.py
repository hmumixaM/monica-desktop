"""Shared import services; transaction ownership stays with the caller."""
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from .content import get_or_create_content
from .import_common import _content_value
from .schemas import ImportBatchRequest


def _ensure_profile_for_record(db: Session, record, request: ImportBatchRequest, content: Any):
    """Create the first local profile projection for a source-native contact.

    Matching an existing person remains an explicit human decision. This helper
    only creates a source-scoped pending profile when a record has no confirmed
    profile ID, so an import can be replayed without losing the original
    identity or conflating two people.
    """
    if getattr(record,'source_is_authoritative',False):return record.profile_id
    if record.profile_id:
        return record.profile_id
    if request.stream not in {"contacts", "profiles", "groups", "people", "legacy"}:
        return None
    normalized = _content_value(content)
    external_id = record.external_id
    provider = "wechat_group" if request.source == "wechat" and request.stream == "groups" else request.source
    identity = (
        db.execute(
            text("""
              SELECT profile_id FROM identities
              WHERE provider = :provider AND external_id = :external_id AND status NOT IN ('invalid','merged')
            """),
            {"provider": provider, "external_id": external_id},
        )
        .mappings()
        .one_or_none()
    )
    if identity and identity["profile_id"]:
        return identity["profile_id"]
    display_name = str(
        normalized.get("display_name")
        or normalized.get("full_name")
        or normalized.get("nickname")
        or normalized.get("nick")
        or normalized.get("remark")
        or external_id
    )
    profile_type = (
        "group"
        if request.stream == "groups" or normalized.get("profile_type") == "group"
        else "person"
    )
    profile_id = db.execute(
        text("""
          INSERT INTO profiles(profile_type, display_name, summary)
          VALUES (:profile_type, :display_name, :summary) RETURNING id
        """),
        {
            "profile_type": profile_type,
            "display_name": display_name,
            "summary": normalized.get("summary") or normalized.get("headline"),
        },
    ).scalar_one()
    db.execute(
        text("""
          INSERT INTO identities(profile_id, provider, external_id, username, profile_url, display_name, status)
          VALUES (:profile_id, :provider, :external_id, :username, :profile_url, :display_name, 'pending')
          ON CONFLICT(provider, external_id) DO UPDATE SET
            profile_id = COALESCE(identities.profile_id, EXCLUDED.profile_id),
            username = COALESCE(EXCLUDED.username, identities.username),
            profile_url = COALESCE(EXCLUDED.profile_url, identities.profile_url),
            display_name = COALESCE(EXCLUDED.display_name, identities.display_name),
            last_seen_at = now()
        """),
        {
            "profile_id": profile_id,
            "provider": provider,
            "external_id": external_id,
            "username": normalized.get("username")
            or normalized.get("alias")
            or normalized.get("u"),
            "profile_url": normalized.get("profile_url"),
            "display_name": display_name,
        },
    )
    return profile_id


def _structured_values(normalized: dict[str, Any], keys: tuple[str, ...]) -> list[Any]:
    for key in keys:
        if key not in normalized or normalized[key] in (None, "", []):
            continue
        value = normalized[key]
        return value if isinstance(value, list) else [value]
    return []


def _entity_id(db: Session, table: str, name: str | None) -> str | None:
    if not name:
        return None
    normalized_name = " ".join(str(name).strip().lower().split())
    if not normalized_name:
        return None
    return db.execute(
        text(f"""
          INSERT INTO {table}(name, normalized_name)
          VALUES (:name, :normalized_name)
          ON CONFLICT (normalized_name) DO UPDATE SET name = EXCLUDED.name
          RETURNING id
        """),
        {"name": str(name).strip(), "normalized_name": normalized_name},
    ).scalar_one()


def _project_structured_fact(
    db: Session,
    *,
    profile_id: str,
    fact_type: str,
    value: Any,
    source_type: str,
    source_record_id: str,
    observed_at: datetime,
    source_account_id=None,
    fact_key_override=None,
) -> None:
    table_by_type = {
        "address": "profile_address_revisions",
        "employment": "profile_employment_revisions",
        "education": "profile_education_revisions",
    }
    revision_table = table_by_type[fact_type]
    from .structured_facts import fact_key as source_fact_key
    fact_key = fact_key_override or source_fact_key(source_type,source_record_id,fact_type,value)

    if source_account_id:
        from .platform_store import observe, platform_engine
        batch=db.info.get('structured_source_batch')
        if batch is not None:
            stack,connections=batch
            if source_type not in connections:
                connections[source_type]=stack.enter_context(platform_engine(source_type).begin())
            source=connections[source_type]
            observe(source,source_account_id,fact_type,fact_key,value,
                    source_event='fact:'+observed_at.isoformat()+':'+source_record_id,observed_at=observed_at)
        else:
            with platform_engine(source_type).begin() as source:
                observe(source,source_account_id,fact_type,fact_key,value,
                        source_event='fact:'+observed_at.isoformat()+':'+source_record_id,observed_at=observed_at)

    content_id, digest = get_or_create_content(
        db, value=value, content_kind=f"structured_{fact_type}"
    )
    current = (
        db.execute(
            text("""
              SELECT current_revision_id, current_content_hash, current_source_type
              FROM profile_structured_current
              WHERE profile_id = :profile_id AND fact_type = :fact_type AND fact_key = :fact_key
            """),
            {"profile_id": profile_id, "fact_type": fact_type, "fact_key": fact_key},
        )
        .mappings()
        .one_or_none()
    )
    latest_provider_hash = db.execute(
        text(f"""
          SELECT content_hash FROM {revision_table}
          WHERE source_type = :source_type AND fact_key = :fact_key AND (
            (CAST(:account AS uuid) IS NOT NULL AND source_account_id=:account) OR
            (CAST(:account AS uuid) IS NULL AND profile_id=:profile_id))
          ORDER BY revision_seq DESC LIMIT 1
        """),
        {"profile_id": profile_id, "source_type": source_type, "fact_key": fact_key,"account":source_account_id},
    ).scalar_one_or_none()
    if current and current["current_content_hash"] == digest:
        return
    if source_type != "manual" and latest_provider_hash == digest:
        return
    previous_revision_id = current["current_revision_id"] if current else None
    is_conflict = bool(
        current and current["current_source_type"] == "manual" and source_type != "manual"
    )
    revision_params: dict[str, Any] = {
        "profile_id": profile_id,
        "fact_key": fact_key,
        "content_id": content_id,
        "digest": digest,
        "source_type": source_type,
        "source_record_id": source_record_id,
        "observed_at": observed_at,
        "previous_revision_id": previous_revision_id,
        "is_conflict": is_conflict,
    }
    if fact_type == "address":
        revision_id = db.execute(
            text("""
              INSERT INTO profile_address_revisions
                (profile_id, fact_key, content_object_id, content_hash, source_type, source_record_id,
                 observed_at, previous_revision_id, is_conflict)
              VALUES (:profile_id, :fact_key, :content_id, :digest, :source_type, :source_record_id,
                      :observed_at, :previous_revision_id, :is_conflict)
              RETURNING id
            """),
            revision_params,
        ).scalar_one()
    else:
        entity_table = "companies" if fact_type == "employment" else "schools"
        name_keys = (
            ("company", "company_name", "employer", "name")
            if fact_type == "employment"
            else (
                "school",
                "school_name",
                "institution",
                "name",
            )
        )
        entity_name = (
            value
            if isinstance(value, str)
            else next(
                (value.get(key) for key in name_keys if isinstance(value, dict) and value.get(key)),
                None,
            )
        )
        revision_params["entity_id"] = _entity_id(db, entity_table, entity_name)
        revision_id = db.execute(
            text(f"""
              INSERT INTO {revision_table}
                (profile_id, fact_key, {"company_id" if fact_type == "employment" else "school_id"},
                 content_object_id, content_hash, source_type, source_record_id,
                 observed_at, previous_revision_id, is_conflict)
              VALUES (:profile_id, :fact_key, :entity_id, :content_id, :digest, :source_type,
                      :source_record_id, :observed_at, :previous_revision_id, :is_conflict)
              RETURNING id
            """),
            revision_params,
        ).scalar_one()
    should_project = (
        not current or source_type == "manual" or current["current_source_type"] != "manual"
    )
    if source_account_id:
        db.execute(text(f'UPDATE {revision_table} SET source_account_id=:account WHERE id=:id'),{'account':source_account_id,'id':revision_id})
        db.execute(text('''INSERT INTO source_account_facts(account_id,fact_type,fact_key,revision_id) VALUES(:account,:type,:key,:id)
            ON CONFLICT(account_id,fact_type,fact_key) DO UPDATE SET revision_id=EXCLUDED.revision_id'''),
            {'account':source_account_id,'type':fact_type,'key':fact_key,'id':revision_id})
    if should_project and profile_id:
        db.execute(
            text("""
              INSERT INTO profile_structured_current
                (profile_id, fact_type, fact_key, current_revision_id, current_content_hash,
                 current_source_type, updated_at)
              VALUES (:profile_id, :fact_type, :fact_key, :revision_id, :digest, :source_type, :updated_at)
              ON CONFLICT (profile_id, fact_type, fact_key) DO UPDATE SET
                current_revision_id = EXCLUDED.current_revision_id,
                current_content_hash = EXCLUDED.current_content_hash,
                current_source_type = EXCLUDED.current_source_type,
                updated_at = EXCLUDED.updated_at
            """),
            {
                "profile_id": profile_id,
                "fact_type": fact_type,
                "fact_key": fact_key,
                "revision_id": revision_id,
                "digest": digest,
                "source_type": source_type,
                "updated_at": observed_at,
            },
        )


def _project_structured_profile(
    db: Session, record, request: ImportBatchRequest, content: Any
) -> None:
    if (
        (not record.profile_id and not getattr(record,'source_account_id',None))
        or request.stream not in {"contacts", "profiles", "people", "legacy"}
        or not isinstance(content, dict)
    ):
        return
    normalized = _content_value(content)
    source_type = request.source
    from .structured_facts import collection_keys
    for kind,keys in (
        ('address',('address','location','locations','addresses')),
        ('employment',('employment','experience','experiences','work_history','work')),
        ('education',('education','educations','education_history','schools')),
    ):
        values=_structured_values(normalized,keys)
        if not values:continue
        account=getattr(record,'source_account_id',None)
        for value,key in collection_keys(db,source=source_type,record=record.external_id,kind=kind,
                values=values,account=account,profile=record.profile_id):
            _project_structured_fact(db,profile_id=record.profile_id,fact_type=kind,value=value,
                source_type=source_type,source_record_id=record.external_id,observed_at=request.observed_at,
                source_account_id=account,fact_key_override=key)
