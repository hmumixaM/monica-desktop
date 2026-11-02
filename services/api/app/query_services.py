"""Read services shared by HTTP routes and aggregate warmup."""
from uuid import UUID

from fastapi import (
    HTTPException,
)
from sqlalchemy import text
from sqlalchemy.orm import Session

from .config import get_settings
from .profile_facts import avatar_map


def profile_relationships(profile_id: UUID, db: Session=None):
    rows = list(
        db.execute(
            text("""
              SELECT e.id, e.from_profile_id, e.to_profile_id, rt.name AS relationship_type,
                     source.display_name AS from_display_name,
                     target.display_name AS to_display_name,
                     CASE WHEN e.from_profile_id = :profile_id THEN 'outgoing' ELSE 'incoming' END AS direction,
                     CASE WHEN e.from_profile_id = :profile_id THEN target.id ELSE source.id END AS related_profile_id,
                     CASE WHEN e.from_profile_id = :profile_id THEN target.display_name ELSE source.display_name END AS related_display_name,
                     CASE WHEN e.from_profile_id = :profile_id THEN target.profile_type ELSE source.profile_type END AS related_profile_type,
                     e.note, e.confidence, e.valid_from, e.valid_to, e.source_type
              FROM relationship_edges e
              JOIN relationship_types rt ON rt.id = e.relationship_type_id
              JOIN profiles source ON source.id = e.from_profile_id
              JOIN profiles target ON target.id = e.to_profile_id
              WHERE (e.from_profile_id = :profile_id OR e.to_profile_id = :profile_id)
                AND source.archived_at IS NULL AND target.archived_at IS NULL
                AND source.profile_type='person' AND target.profile_type='person'
                AND e.evidence_kind='relationship'
                AND rt.name NOT IN (
                  'wechat_group', 'wechat_group_member', 'wechat_group_owner',
                  'wechat_group_owned_by', '微信群', '群成员', '群主',
                  '微信名片推荐'
                )
              ORDER BY rt.name, e.created_at DESC
            """),
            {"profile_id": profile_id},
        ).mappings()
    )
    # Group memberships are a first-class relationship in Monica's WeChat
    # model.  Keep them visible in the same API as hand-authored edges so a
    # group Profile has clickable members and a person has clickable groups.
    memberships = list(
        db.execute(
            text("""
              SELECT gm.group_profile_id, gm.person_profile_id, gm.role,
                     groups.display_name AS group_display_name,
                     groups.profile_type AS group_profile_type,
                     people.display_name AS person_display_name,
                     people.profile_type AS person_profile_type,
                     gm.observed_at
              FROM group_memberships gm
              JOIN profiles groups ON groups.id = gm.group_profile_id
                AND groups.profile_type = 'group'
              JOIN profiles people ON people.id = gm.person_profile_id
                AND people.profile_type = 'person'
              WHERE (gm.group_profile_id = :profile_id OR gm.person_profile_id = :profile_id)
                AND gm.group_profile_id <> gm.person_profile_id AND gm.left_at IS NULL
                AND groups.archived_at IS NULL AND people.archived_at IS NULL
              ORDER BY gm.observed_at DESC
            """),
            {"profile_id": profile_id},
        ).mappings()
    )
    for membership in memberships:
        is_group = membership["group_profile_id"] == profile_id
        related_id = membership["person_profile_id"] if is_group else membership["group_profile_id"]
        role = membership["role"] or ("群成员" if is_group else "微信群")
        rows.append(
            {
                "id": f"membership:{membership['group_profile_id']}:{membership['person_profile_id']}",
                "from_profile_id": membership["group_profile_id"],
                "to_profile_id": membership["person_profile_id"],
                "relationship_type": {"owner":"群主", "admin":"管理员", "member":"群成员"}.get(str(role).lower(), role),
                "from_display_name": membership["group_display_name"],
                "to_display_name": membership["person_display_name"],
                "direction": "outgoing" if is_group else "incoming",
                "related_profile_id": related_id,
                "related_display_name": membership["person_display_name"] if is_group else membership["group_display_name"],
                "related_profile_type": membership["person_profile_type"] if is_group else membership["group_profile_type"],
                "note": None,
                "confidence": 1,
                "valid_from": None,
                "valid_to": None,
                "source_type": "wechat",
            }
        )
    avatars = avatar_map(db, [row["related_profile_id"] for row in rows])
    rows = [{**row, "avatar_media_id": avatars.get(str(row["related_profile_id"]))} for row in rows]
    return rows


def list_tags(profile_type: str | None=None, db: Session=None):
    if profile_type and profile_type not in ('person','group'):raise HTTPException(status_code=422,detail='Unknown profile type')
    return list(db.execute(text("""SELECT t.id,t.name,count(DISTINCT p.id) AS people,
        array_remove(array_agg(DISTINCT m.source_type) FILTER(WHERE p.id IS NOT NULL),NULL) AS sources
        FROM tags t LEFT JOIN tag_memberships m ON m.tag_id=t.id
        LEFT JOIN profiles p ON p.id=m.profile_id AND p.archived_at IS NULL
            AND (CAST(:kind AS text) IS NULL OR p.profile_type=:kind)
        GROUP BY t.id,t.name ORDER BY t.name,t.id"""),{'kind':profile_type}).mappings())


def metrics_summary(db: Session=None):
    counts = (
        db.execute(
            text("""
          SELECT
            (SELECT count(*) FROM profiles WHERE profile_type = 'person' AND archived_at IS NULL) AS people,
            (SELECT count(*) FROM profiles WHERE profile_type = 'group' AND archived_at IS NULL) AS groups,
            (SELECT count(*) FROM identities WHERE status = 'active') AS identities,
            (SELECT count(*) FROM identities WHERE provider = 'wechat' AND status = 'active') AS wechat_identities,
            (SELECT count(*) FROM identities WHERE provider = 'instagram' AND status = 'active') AS instagram_identities,
            (SELECT count(*) FROM identities WHERE provider = 'linkedin' AND status = 'active') AS linkedin_identities,
            (SELECT count(*) FROM source_observations WHERE source = 'wechat' AND stream = 'moments') AS wechat_moments,
            (SELECT count(*) FROM group_memberships) AS group_memberships,
            (SELECT count(*) FROM media_assets) AS media,
            (SELECT count(*) FROM social_posts) AS posts,
            (SELECT count(*) FROM social_stories) AS stories,
            (SELECT count(*) FROM relationship_edges) AS relationships,
            (SELECT count(*) FROM activities) AS activities,
            (SELECT count(*) FROM companies) AS companies,
            (SELECT count(*) FROM schools) AS schools,
            (SELECT count(*) FROM profile_address_revisions) AS address_revisions,
            (SELECT count(*) FROM source_observations WHERE state = 'unchanged') AS unchanged_observations,
            (SELECT count(*) FROM profile_field_revisions) AS revisions
        """)
        )
        .mappings()
        .one()
    )
    return dict(counts)


def relationship_graph(limit: int=900, source_type: str | None=None, relationship_type: str | None=None, min_confidence: float=0, format: str | None=None, focus: UUID | None=None, db: Session=None):
    settings = get_settings()
    """Return a bounded, name-resolved graph projection for the relationship UI.

    The database remains the source of truth for the full edge set.  The web
    graph only receives the strongest/recent slice requested by the user so a
    16k-edge archive never blocks a normal Profile request.
    """
    if format == "monica":
        from .graph import graph_projection
        return graph_projection(db, settings.raw_root, limit, focus)

    clauses = ["1 = 1"]
    params: dict[str, object] = {"limit": min(max(limit, 50), 5000)}
    if source_type:
        clauses.append("e.source_type = :source_type")
        params["source_type"] = source_type
    if relationship_type:
        clauses.append("rt.name = :relationship_type")
        params["relationship_type"] = relationship_type
    if min_confidence > 0:
        clauses.append("COALESCE(e.confidence, 0) >= :min_confidence")
        params["min_confidence"] = min(min_confidence, 1)
    edges = list(
        db.execute(
            text(f"""
              SELECT e.id, e.from_profile_id, e.to_profile_id,
                     source.display_name AS from_display_name,
                     source.profile_type AS from_profile_type,
                     target.display_name AS to_display_name,
                     target.profile_type AS to_profile_type,
                     rt.name AS relationship_type, e.source_type,
                     e.confidence, e.note, e.created_at
              FROM relationship_edges e
              JOIN profiles source ON source.id = e.from_profile_id
              JOIN profiles target ON target.id = e.to_profile_id
              JOIN relationship_types rt ON rt.id = e.relationship_type_id
              WHERE {" AND ".join(clauses)}
              ORDER BY COALESCE(e.confidence, 0) DESC, e.created_at DESC, e.id DESC
              LIMIT :limit
            """),
            params,
        ).mappings()
    )
    node_map: dict[str, dict[str, object]] = {}
    for edge in edges:
        for prefix in ("from", "to"):
            node_id = str(edge[f"{prefix}_profile_id"])
            node = node_map.setdefault(
                node_id,
                {
                    "id": edge[f"{prefix}_profile_id"],
                    "display_name": edge[f"{prefix}_display_name"],
                    "profile_type": edge[f"{prefix}_profile_type"],
                    "degree": 0,
                },
            )
            node["degree"] = int(node["degree"]) + 1
    total_edges = db.execute(
        text(f"""
          SELECT count(*) FROM relationship_edges e
          JOIN relationship_types rt ON rt.id = e.relationship_type_id
          WHERE {" AND ".join(clauses)}
        """),
        params,
    ).scalar_one()
    total_nodes = db.execute(
        text("SELECT count(*) FROM profiles WHERE archived_at IS NULL")
    ).scalar_one()
    type_counts = list(
        db.execute(
            text("""
              SELECT rt.name AS relationship_type, count(*) AS edge_count
              FROM relationship_edges e JOIN relationship_types rt ON rt.id = e.relationship_type_id
              GROUP BY rt.name ORDER BY edge_count DESC, rt.name
            """)
        ).mappings()
    )
    source_counts = list(
        db.execute(
            text("""
              SELECT source_type, count(*) AS edge_count
              FROM relationship_edges GROUP BY source_type ORDER BY edge_count DESC, source_type
            """)
        ).mappings()
    )
    return {
        "nodes": list(node_map.values()),
        "edges": edges,
        "total_edges": total_edges,
        "total_nodes": total_nodes,
        "visible_edges": len(edges),
        "relationship_types": type_counts,
        "sources": source_counts,
    }


def people_overview(db: Session=None):
    counts = db.execute(text("SELECT count(*) FILTER(WHERE profile_type='person') AS people, count(*) FILTER(WHERE profile_type='group') AS groups FROM profiles WHERE archived_at IS NULL")).mappings().one()
    result = dict(counts)
    for name, table, entity, fk in [('companies','profile_employment_revisions','companies','company_id'),('schools','profile_education_revisions','schools','school_id')]:
        result[name] = list(db.execute(text(f"""SELECT e.name,count(DISTINCT c.profile_id) AS count
            FROM profile_structured_current c JOIN {table} r ON r.id=c.current_revision_id
            JOIN {entity} e ON e.id=r.{fk} JOIN profiles p ON p.id=c.profile_id AND p.archived_at IS NULL
            GROUP BY e.name ORDER BY count DESC,e.name LIMIT 8""")).mappings())
    return result
