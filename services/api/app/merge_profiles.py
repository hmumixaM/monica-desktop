"""Safely merge duplicate source profiles into the canonical Monica profiles.

The first import pass can create a provider profile before the Monica export is
loaded.  This utility links only high-confidence matches to the later Monica
profile.  It keeps every source revision/content object and archives the old
profile instead of deleting it, so the operation can be audited or reversed.

Run without ``--apply`` to inspect the proposed mapping.  The production
command is ``python -m app.merge_profiles --apply``.
"""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

from .db import SessionLocal

CANONICAL_CUTOFF = datetime(2026, 9, 18, 12, 20, tzinfo=UTC)
GENERIC_VALUES = {
    "unknown",
    "none",
    "null",
    "undefined",
    "未记录来源",
    "扫一扫二维码",
    "微信好友",
    "好友",
}


def normalized(value: Any) -> str:
    if value is None:
        return ""
    value = unicodedata.normalize("NFKC", str(value)).strip().casefold()
    return re.sub(r"\s+", " ", value)


def useful_value(value: Any) -> bool:
    value = normalized(value)
    return len(value) >= 3 and value not in GENERIC_VALUES


def scalar_strings(value: Any) -> list[str]:
    """Flatten JSON values without turning large objects into noisy tokens."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(scalar_strings(item))
        return result
    if isinstance(value, dict):
        result = []
        for item in value.values():
            result.extend(scalar_strings(item))
        return result
    return []


@dataclass
class Evidence:
    score: int = 0
    reasons: list[str] = field(default_factory=list)
    strong: bool = False

    def add(self, score: int, reason: str, strong: bool = False) -> None:
        self.score += score
        if reason not in self.reasons:
            self.reasons.append(reason)
        self.strong = self.strong or strong


def load_match_inputs(db):
    profiles = {
        row["id"]: dict(row)
        for row in db.execute(
            text(
                """
                SELECT id, profile_type, display_name, summary, avatar_media_id, created_at
                FROM profiles WHERE archived_at IS NULL
                """
            )
        ).mappings()
    }
    identities = list(
        db.execute(
            text(
                """
                SELECT id, profile_id, provider, external_id, username, profile_url,
                       display_name, status
                FROM identities WHERE profile_id IS NOT NULL
                """
            )
        ).mappings()
    )
    fields: dict[UUID, dict[str, list[Any]]] = defaultdict(dict)
    for row in db.execute(
        text(
            """
            SELECT c.profile_id, c.field_key, co.payload_json AS value
            FROM profile_field_current c
            JOIN profile_field_revisions r ON r.id = c.current_revision_id
            JOIN content_objects co ON co.id = r.content_object_id
            """
        )
    ).mappings():
        fields[row["profile_id"]][row["field_key"]] = scalar_strings(row["value"])
    return profiles, identities, fields


def proposed_merges(db) -> tuple[list[tuple[UUID, UUID, Evidence]], dict[str, int]]:
    profiles, identities, fields = load_match_inputs(db)
    canonical_ids = {
        row["profile_id"]
        for row in identities
        if row["provider"] == "monica" and row["profile_id"] in profiles
    }
    canonical_ids = {
        profile_id
        for profile_id in canonical_ids
        if profiles[profile_id]["profile_type"] == "person"
    }
    canonical_identity_index: dict[tuple[str, str, str], set[UUID]] = defaultdict(set)
    canonical_global_index: dict[str, set[UUID]] = defaultdict(set)
    canonical_name_index: dict[str, set[UUID]] = defaultdict(set)
    identity_by_profile: dict[UUID, list[dict[str, Any]]] = defaultdict(list)
    for identity in identities:
        profile_id = identity["profile_id"]
        if profile_id not in canonical_ids:
            continue
        identity_by_profile[profile_id].append(dict(identity))
        canonical_name_index[normalized(identity["display_name"])].add(profile_id)
        for kind in ("external_id", "username", "profile_url", "display_name"):
            value = normalized(identity[kind])
            if not useful_value(value):
                continue
            canonical_identity_index[(identity["provider"], kind, value)].add(profile_id)
            canonical_global_index[value].add(profile_id)
    for profile_id in canonical_ids:
        name = normalized(profiles[profile_id]["display_name"])
        if useful_value(name):
            canonical_name_index[name].add(profile_id)

    source_ids = [
        profile_id
        for profile_id, profile in profiles.items()
        if profile["profile_type"] == "person"
        and profile_id not in canonical_ids
        and profile["created_at"] < CANONICAL_CUTOFF
    ]
    source_identity_by_profile: dict[UUID, list[dict[str, Any]]] = defaultdict(list)
    for identity in identities:
        if identity["profile_id"] in source_ids:
            source_identity_by_profile[identity["profile_id"]].append(dict(identity))

    proposals: list[tuple[UUID, UUID, Evidence]] = []
    stats = {
        "canonical_profiles": len(canonical_ids),
        "source_profiles": len(source_ids),
        "matched": 0,
        "ambiguous": 0,
        "unmatched": 0,
        "strong_matches": 0,
    }
    for source_id in source_ids:
        source = profiles[source_id]
        candidates: dict[UUID, Evidence] = defaultdict(Evidence)

        def add_for_index(
            provider: str | None,
            kind: str,
            value: Any,
            weight: int,
            reason: str,
            strong=False,
            candidate_map=candidates,
        ):
            value_norm = normalized(value)
            if not useful_value(value_norm):
                return
            if provider:
                targets = canonical_identity_index.get((provider, kind, value_norm), set())
            else:
                targets = canonical_global_index.get(value_norm, set())
            for candidate in targets:
                candidate_map[candidate].add(weight, reason, strong)

        # Provider identities are the strongest evidence when the source
        # importer and the Monica export used different external id formats.
        for identity in source_identity_by_profile.get(source_id, []):
            provider = identity["provider"]
            for kind, weight in (("profile_url", 22), ("external_id", 20), ("username", 18)):
                add_for_index(
                    provider, kind, identity[kind], weight, f"identity:{provider}:{kind}", True
                )
            add_for_index(
                provider, "display_name", identity["display_name"], 8, f"identity:{provider}:name"
            )

        # Fields produced by the provider importers are useful even when the
        # first pass did not create an identity row.
        for field_key, values in fields.get(source_id, {}).items():
            provider = field_key.split(".", 1)[0] if "." in field_key else None
            if field_key == "linkedin.profile_url":
                for value in values:
                    add_for_index(
                        "linkedin", "profile_url", value, 30, "field:linkedin.profile_url", True
                    )
                    add_for_index(
                        "linkedin", "external_id", value, 30, "field:linkedin.profile_url", True
                    )
            elif field_key == "wechat.alias":
                for value in values:
                    add_for_index("wechat", "username", value, 28, "field:wechat.alias", True)
                    add_for_index("wechat", "external_id", value, 28, "field:wechat.alias", True)
            elif field_key == "profile_pic_url":
                for value in values:
                    add_for_index(None, "profile_url", value, 18, "field:profile_pic_url", True)
            elif field_key in {"wechat.nickname", "wechat.remark", "wechat.historical_nicknames"}:
                for value in values:
                    add_for_index("wechat", "display_name", value, 10, f"field:{field_key}")
                    add_for_index("wechat", "username", value, 10, f"field:{field_key}")
                    add_for_index(None, "display_name", value, 6, f"field:{field_key}")

        # Exact display name is useful only as supporting evidence; common
        # names stay ambiguous until another independent value agrees.
        name = normalized(source["display_name"])
        if useful_value(name):
            for candidate in canonical_name_index.get(name, set()):
                candidates[candidate].add(6, "display_name")

        if not candidates:
            stats["unmatched"] += 1
            continue
        ranked = sorted(
            candidates.items(), key=lambda item: (item[1].score, len(item[1].reasons)), reverse=True
        )
        best_id, best = ranked[0]
        runner_score = ranked[1][1].score if len(ranked) > 1 else -1
        independent = len(best.reasons)
        accepted = (best.strong and best.score >= 18 and best.score - runner_score >= 5) or (
            not best.strong
            and best.score >= 14
            and independent >= 2
            and best.score - runner_score >= 4
        )
        if not accepted:
            stats["ambiguous"] += 1
            continue
        stats["matched"] += 1
        if best.strong:
            stats["strong_matches"] += 1
        proposals.append((source_id, best_id, best))
    return proposals, stats


def _move_current_rows(db, table: str, source_id: UUID, target_id: UUID, key: str) -> None:
    source_rows = list(
        db.execute(
            text(f"SELECT {key} FROM {table} WHERE profile_id = :source"),
            {"source": source_id},
        ).scalars()
    )
    for value in source_rows:
        exists = db.execute(
            text(f"SELECT 1 FROM {table} WHERE profile_id = :target AND {key} = :value"),
            {"target": target_id, "value": value},
        ).scalar_one_or_none()
        if exists:
            db.execute(
                text(f"DELETE FROM {table} WHERE profile_id = :source AND {key} = :value"),
                {"source": source_id, "value": value},
            )
        else:
            db.execute(
                text(
                    f"UPDATE {table} SET profile_id = :target WHERE profile_id = :source AND {key} = :value"
                ),
                {"target": target_id, "source": source_id, "value": value},
            )


def _merge_edges(db, source_id: UUID, target_id: UUID) -> None:
    edges = list(
        db.execute(
            text(
                """
                SELECT id, from_profile_id, to_profile_id, relationship_type_id
                FROM relationship_edges
                WHERE from_profile_id = :source OR to_profile_id = :source
                """
            ),
            {"source": source_id},
        ).mappings()
    )
    for edge in edges:
        new_from = target_id if edge["from_profile_id"] == source_id else edge["from_profile_id"]
        new_to = target_id if edge["to_profile_id"] == source_id else edge["to_profile_id"]
        if new_from == new_to:
            db.execute(text("DELETE FROM relationship_edges WHERE id = :id"), {"id": edge["id"]})
            continue
        existing = db.execute(
            text(
                """
                SELECT id FROM relationship_edges
                WHERE from_profile_id = :from_id AND to_profile_id = :to_id
                  AND relationship_type_id = :relationship_type_id
                """
            ),
            {
                "from_id": new_from,
                "to_id": new_to,
                "relationship_type_id": edge["relationship_type_id"],
            },
        ).scalar_one_or_none()
        if existing:
            db.execute(
                text(
                    "UPDATE relationship_edge_revisions SET edge_id = :new_id WHERE edge_id = :old_id"
                ),
                {"new_id": existing, "old_id": edge["id"]},
            )
            db.execute(text("DELETE FROM relationship_edges WHERE id = :id"), {"id": edge["id"]})
        else:
            db.execute(
                text(
                    """
                    UPDATE relationship_edges
                    SET from_profile_id = :from_id, to_profile_id = :to_id
                    WHERE id = :id
                    """
                ),
                {"id": edge["id"], "from_id": new_from, "to_id": new_to},
            )


def _merge_conversations(db, source_id: UUID, target_id: UUID) -> None:
    conversations = list(
        db.execute(
            text(
                "SELECT id, conversation_type, external_id FROM conversations WHERE profile_id = :source"
            ),
            {"source": source_id},
        ).mappings()
    )
    for conversation in conversations:
        existing = db.execute(
            text(
                """
                SELECT id FROM conversations
                WHERE conversation_type = :conversation_type
                  AND external_id IS NOT DISTINCT FROM :external_id
                  AND id <> :id
                """
            ),
            {**conversation, "id": conversation["id"]},
        ).scalar_one_or_none()
        if existing:
            db.execute(
                text(
                    "UPDATE message_events SET conversation_id = :new_id WHERE conversation_id = :old_id"
                ),
                {"new_id": existing, "old_id": conversation["id"]},
            )
            db.execute(text("DELETE FROM conversations WHERE id = :id"), {"id": conversation["id"]})
        else:
            db.execute(
                text("UPDATE conversations SET profile_id = :target WHERE id = :source"),
                {"target": target_id, "source": conversation["id"]},
            )


def _merge_profile(db, source_id: UUID, target_id: UUID) -> None:
    db.execute(
        text(
            """
            UPDATE profiles target SET
              avatar_media_id = COALESCE(target.avatar_media_id, source.avatar_media_id),
              summary = COALESCE(NULLIF(target.summary, ''), source.summary)
            FROM profiles source
            WHERE target.id = :target_id AND source.id = :source_id
            """
        ),
        {"source_id": source_id, "target_id": target_id},
    )

    # Keep the historical identity row even if the target already has the
    # same provider key.  A merged row remains auditable but is hidden from
    # the active identity projection.
    source_identities = list(
        db.execute(
            text("SELECT id, provider, external_id FROM identities WHERE profile_id = :source"),
            {"source": source_id},
        ).mappings()
    )
    for identity in source_identities:
        conflict = db.execute(
            text(
                "SELECT id FROM identities WHERE provider = :provider AND external_id = :external_id AND profile_id = :target"
            ),
            {
                "provider": identity["provider"],
                "external_id": identity["external_id"],
                "target": target_id,
            },
        ).scalar_one_or_none()
        if conflict:
            db.execute(
                text("UPDATE identities SET status = 'merged' WHERE id = :id"),
                {"id": identity["id"]},
            )
        else:
            db.execute(
                text(
                    "UPDATE identities SET profile_id = :target WHERE id = :id"
                ),
                {"target": target_id, "id": identity["id"]},
            )
    db.execute(
        text(
            "UPDATE identity_candidates SET candidate_profile_id = :target WHERE candidate_profile_id = :source"
        ),
        {"target": target_id, "source": source_id},
    )

    db.execute(
        text("UPDATE profile_field_revisions SET profile_id = :target WHERE profile_id = :source"),
        {"target": target_id, "source": source_id},
    )
    _move_current_rows(db, "profile_field_current", source_id, target_id, "field_key")

    for table in (
        "profile_address_revisions",
        "profile_employment_revisions",
        "profile_education_revisions",
    ):
        db.execute(
            text(f"UPDATE {table} SET profile_id = :target WHERE profile_id = :source"),
            {"target": target_id, "source": source_id},
        )
    db.execute(text("""DELETE FROM profile_structured_current s USING profile_structured_current t
        WHERE s.profile_id=:source AND t.profile_id=:target
          AND s.fact_type=t.fact_type AND s.fact_key=t.fact_key"""), {"source":source_id,"target":target_id})
    db.execute(text("UPDATE profile_structured_current SET profile_id=:target WHERE profile_id=:source"),
               {"source":source_id,"target":target_id})

    for table, column in (
        ("social_posts", "profile_id"),
        ("social_stories", "profile_id"),
        ("conversations", "profile_id"),
    ):
        if table == "conversations":
            _merge_conversations(db, source_id, target_id)
        else:
            db.execute(
                text(f"UPDATE {table} SET {column} = :target WHERE {column} = :source"),
                {"target": target_id, "source": source_id},
            )
    db.execute(text("UPDATE social_interactions SET author_profile_id=:target WHERE author_profile_id=:source"),
               {"source":source_id,"target":target_id})
    db.execute(
        text(
            "UPDATE social_comments SET author_profile_id = :target WHERE author_profile_id = :source"
        ),
        {"target": target_id, "source": source_id},
    )
    db.execute(
        text(
            "UPDATE message_events SET sender_profile_id = :target WHERE sender_profile_id = :source"
        ),
        {"target": target_id, "source": source_id},
    )

    db.execute(
        text(
            """
            INSERT INTO group_memberships(group_profile_id, person_profile_id, role, joined_at, left_at, observed_at, source_record_id)
            SELECT CASE WHEN group_profile_id=:source THEN :target ELSE group_profile_id END,
                   CASE WHEN person_profile_id=:source THEN :target ELSE person_profile_id END,
                   role, joined_at, left_at, observed_at, source_record_id
            FROM group_memberships WHERE person_profile_id=:source OR group_profile_id=:source
            ON CONFLICT (group_profile_id, person_profile_id) DO NOTHING
            """
        ),
        {"source": source_id, "target": target_id},
    )
    db.execute(
        text("DELETE FROM group_memberships WHERE person_profile_id = :source OR group_profile_id=:source"),
        {"source": source_id},
    )
    db.execute(
        text(
            """
            INSERT INTO activity_participants(activity_id, profile_id)
            SELECT activity_id, :target FROM activity_participants WHERE profile_id = :source
            ON CONFLICT (activity_id, profile_id) DO NOTHING
            """
        ),
        {"source": source_id, "target": target_id},
    )
    db.execute(
        text("DELETE FROM activity_participants WHERE profile_id = :source"), {"source": source_id}
    )
    db.execute(
        text(
            """
            INSERT INTO tag_memberships(tag_id, profile_id, source_type, created_at)
            SELECT tag_id, :target, source_type, created_at FROM tag_memberships WHERE profile_id = :source
            ON CONFLICT (tag_id, profile_id) DO NOTHING
            """
        ),
        {"source": source_id, "target": target_id},
    )
    db.execute(
        text("DELETE FROM tag_memberships WHERE profile_id = :source"), {"source": source_id}
    )
    db.execute(
        text("UPDATE tag_membership_revisions SET profile_id = :target WHERE profile_id = :source"),
        {"target": target_id, "source": source_id},
    )
    db.execute(
        text("""
            INSERT INTO tag_membership_removals(tag_id, profile_id, removed_by, removed_at)
            SELECT tag_id, :target, removed_by, removed_at FROM tag_membership_removals WHERE profile_id = :source
            ON CONFLICT (tag_id, profile_id) DO NOTHING
            """),
        {"source": source_id, "target": target_id},
    )

    # Current projections have a natural primary key.  Keep the canonical
    # projection when both profiles already have one and retain all history.
    db.execute(
        text(
            """
            INSERT INTO profile_metrics_current(profile_id, metric_key, value_json, source_watermark, computed_at, calculation_version, freshness_state)
            SELECT :target, metric_key, value_json, source_watermark, computed_at, calculation_version, freshness_state
            FROM profile_metrics_current WHERE profile_id = :source
            ON CONFLICT (profile_id, metric_key) DO NOTHING
            """
        ),
        {"source": source_id, "target": target_id},
    )
    db.execute(
        text("DELETE FROM profile_metrics_current WHERE profile_id = :source"),
        {"source": source_id},
    )
    for entity_type in ("profile",):
        links = list(
            db.execute(
                text(
                    "SELECT media_id, entity_id, role FROM media_links WHERE entity_type = :entity_type AND entity_id = :source"
                ),
                {"entity_type": entity_type, "source": source_id},
            ).mappings()
        )
        for link in links:
            db.execute(
                text(
                    """
                    INSERT INTO media_links(media_id, entity_type, entity_id, role)
                    VALUES (:media_id, :entity_type, :target, :role)
                    ON CONFLICT DO NOTHING
                    """
                ),
                {
                    "media_id": link["media_id"],
                    "entity_type": entity_type,
                    "target": target_id,
                    "role": link["role"],
                },
            )
        db.execute(
            text(
                "DELETE FROM media_links WHERE entity_type = :entity_type AND entity_id = :source"
            ),
            {"entity_type": entity_type, "source": source_id},
        )
    db.execute(
        text(
            "UPDATE search_documents SET entity_id = :target WHERE entity_type = 'profile' AND entity_id = :source"
        ),
        {"target": target_id, "source": source_id},
    )
    db.execute(text("UPDATE source_crosswalk SET profile_id=:target WHERE profile_id=:source"), {"source":source_id,"target":target_id})
    _merge_edges(db, source_id, target_id)
    db.execute(text("UPDATE profile_redirects SET profile_id=:target WHERE profile_id=:source"),
               {"source":source_id,"target":target_id})
    db.execute(text("""INSERT INTO profile_redirects(old_profile_id,profile_id,evidence)
        VALUES (:source,:target,'{"kind":"confirmed-source-identifier"}') ON CONFLICT(old_profile_id)
        DO UPDATE SET profile_id=EXCLUDED.profile_id"""), {"source":source_id,"target":target_id})
    db.execute(
        text("UPDATE profiles SET archived_at = now() WHERE id = :source"), {"source": source_id}
    )
    from .ai.link_changes import refresh_after_link
    refresh_after_link(db, [source_id, target_id])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="apply the proposed archive/merge mapping"
    )
    parser.add_argument("--limit", type=int, default=0, help="only apply the first N proposals")
    args = parser.parse_args()
    db = SessionLocal()
    try:
        proposals, stats = proposed_merges(db)
        print(json.dumps({"stats": stats, "proposals": len(proposals)}, ensure_ascii=False))
        for source, target, evidence in proposals[:20]:
            print(
                json.dumps(
                    {
                        "source": str(source),
                        "target": str(target),
                        "score": evidence.score,
                        "reasons": evidence.reasons,
                    },
                    ensure_ascii=False,
                )
            )
        if not args.apply:
            return
        raise SystemExit("Automatic merging is disabled. Review identity candidates individually in the import center.")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
