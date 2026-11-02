"""亲疏圈: the people you talk to most, on Dunbar rings, grouped into the communities they share.

Both windows are returned together so switching between them only moves people between rings.
"""

from collections import Counter, defaultdict
from datetime import date, datetime
from zoneinfo import ZoneInfo

import networkx as nx
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from .aggregate_cache import cached_endpoint
from .auth import current_user
from .community_names import community_labels
from .config import get_settings
from .data_versions import AI, DOMAINS, FACES
from .db import get_db

router = APIRouter()

WINDOWS = (3, 12)
# Dunbar layers: support clique, sympathy group, close friends, active network.
RINGS = (5, 15, 50, 150)
CALL_WEIGHT = 10
# Groups larger than this say little about who knows whom.
SMALL_GROUP = 80
RELATIONSHIP_WEIGHT = 2.0
SEED = 7
OTHER = "other"


def months_back(today: date, months: int) -> date:
    """The first day of the month `months - 1` months before today's month."""
    index = today.year * 12 + today.month - 1 - (months - 1)
    return date(index // 12, index % 12 + 1, 1)


def ring_of(rank: int) -> int | None:
    return next((ring for ring, size in enumerate(RINGS) if rank < size), None)


def scores(db, today: date) -> list[dict]:
    short, long = (months_back(today, months) for months in WINDOWS)
    return [dict(row) for row in db.execute(text("""
        WITH messages AS (
          SELECT profile_id, sum(sent + received) FILTER (WHERE month >= :short) AS short, sum(sent + received) AS long
          FROM profile_message_months WHERE month >= :long GROUP BY profile_id
        ), calls AS (
          SELECT l.profile_id, count(*) FILTER (WHERE e.occurred_at >= :short) AS short, count(*) AS long
          FROM source_contact_events e JOIN source_account_links l ON l.account_id = e.account_id
          WHERE e.event_kind = 'call' AND e.occurred_at >= :long GROUP BY l.profile_id
        )
        SELECT p.id, p.display_name AS name, profile_avatar_media(p.id, p.avatar_media_id) AS avatar_media_id,
          COALESCE(m.short, 0) + :weight * COALESCE(c.short, 0) AS score_short,
          COALESCE(m.long, 0) + :weight * COALESCE(c.long, 0) AS score_long,
          COALESCE(m.short, 0) AS messages_short, COALESCE(m.long, 0) AS messages_long,
          COALESCE(c.long, 0) AS calls
        FROM profiles p
        LEFT JOIN messages m ON m.profile_id = p.id
        LEFT JOIN calls c ON c.profile_id = p.id
        WHERE p.profile_type = 'person' AND p.archived_at IS NULL AND (m.profile_id IS NOT NULL OR c.profile_id IS NOT NULL)
    """), {"short": short, "long": long, "weight": CALL_WEIGHT}).mappings()]


def place(rows: list[dict]) -> dict[str, dict[int, int]]:
    """Each person's ring per window, for everyone inside the outer ring in at least one window."""
    rings: dict[str, dict[int, int]] = defaultdict(dict)
    for window, key in zip(WINDOWS, ("score_short", "score_long"), strict=True):
        ranked = sorted((row for row in rows if row[key] > 0), key=lambda row: (-row[key], row["name"]))
        for rank, row in enumerate(ranked[:RINGS[-1]]):
            rings[str(row["id"])][window] = ring_of(rank)
    return rings


def communities(db, people: list[str]) -> tuple[dict[str, str], list[dict]]:
    """Louvain communities over shared small groups and confirmed relationships, labelled by their most shared group."""
    memberships = db.execute(text("""
        WITH small AS MATERIALIZED (
          SELECT group_profile_id, count(*) AS size FROM group_memberships WHERE left_at IS NULL
          GROUP BY group_profile_id HAVING count(*) <= :small)
        SELECT m.group_profile_id, g.display_name, small.size, m.person_profile_id
        FROM group_memberships m JOIN small USING (group_profile_id)
        JOIN profiles g ON g.id = m.group_profile_id AND g.archived_at IS NULL AND g.profile_type = 'group'
        WHERE m.left_at IS NULL AND m.person_profile_id = ANY(CAST(:ids AS uuid[]))
    """), {"small": SMALL_GROUP, "ids": people}).all()
    groups: dict[str, dict] = {}
    for group_id, name, size, person in memberships:
        groups.setdefault(str(group_id), {"name": name, "size": size, "members": set()})["members"].add(str(person))
    graph = nx.Graph()
    graph.add_nodes_from(people)
    for group in groups.values():
        members = sorted(group["members"])
        for index, a in enumerate(members):
            for b in members[index + 1:]:
                weight = graph.get_edge_data(a, b, {"weight": 0.0})["weight"]
                graph.add_edge(a, b, weight=weight + 1 / group["size"])
    for a, b in db.execute(text("""SELECT from_profile_id, to_profile_id FROM relationship_edges
            WHERE evidence_kind = 'relationship' AND from_profile_id = ANY(CAST(:ids AS uuid[]))
              AND to_profile_id = ANY(CAST(:ids AS uuid[])) AND from_profile_id <> to_profile_id"""), {"ids": people}):
        a, b = str(a), str(b)
        weight = graph.get_edge_data(a, b, {"weight": 0.0})["weight"]
        graph.add_edge(a, b, weight=weight + RELATIONSHIP_WEIGHT)

    found = [members for members in nx.community.louvain_communities(graph, weight="weight", seed=SEED) if len(members) > 1]
    assignment = dict.fromkeys(people, OTHER)
    result, used = [], Counter()
    for members in sorted(found, key=lambda members: (-len(members), min(members))):
        shared = Counter({group_id: len(group["members"] & members) for group_id, group in groups.items()})
        top = next(((group_id, count) for group_id, count in shared.most_common(1) if count > 1), None)
        key = f"g:{top[0]}" if top else f"c:{min(members)}"
        used[key] += 1
        if used[key] > 1:
            key = f"{key}:{used[key]}"
        result.append({"id": key, "label": groups[top[0]]["name"] if top else "相互认识的一群人", "size": len(members)})
        for person in members:
            assignment[person] = key
    others = sum(1 for value in assignment.values() if value == OTHER)
    if others:
        result.append({"id": OTHER, "label": "其他", "size": others})
    return assignment, result


def build_orbit(db, today: date) -> dict:
    rows = scores(db, today)
    rings = place(rows)
    people = [row for row in rows if str(row["id"]) in rings]
    assignment, groups = communities(db, [str(row["id"]) for row in people])
    last = dict(db.execute(text("""SELECT profile_id, last_contact_at FROM profile_contact_stats
        WHERE profile_id = ANY(CAST(:ids AS uuid[]))"""), {"ids": [str(row["id"]) for row in people]}).all())
    names = community_labels(db)
    return {
        "rings": list(RINGS),
        "windows": list(WINDOWS),
        "communities": [{**group, **names.get(group["id"], {})} for group in groups],
        "people": [{
            "profile_id": str(row["id"]),
            "name": row["name"],
            "avatar_media_id": str(row["avatar_media_id"]) if row["avatar_media_id"] else None,
            "community": assignment[str(row["id"])],
            "last_contact_at": last.get(row["id"]),
            "calls": int(row["calls"]),
            "messages": {str(WINDOWS[0]): int(row["messages_short"]), str(WINDOWS[1]): int(row["messages_long"])},
            "ring": {str(window): rings[str(row["id"])].get(window) for window in WINDOWS},
        } for row in sorted(people, key=lambda row: (-row["score_long"], row["name"]))],
    }


@router.get("/api/v1/insights/orbit")
@cached_endpoint("orbit", (*DOMAINS, AI, FACES), ttl=3600, stale_on_change=True)
def orbit(db: Session = Depends(get_db), _user=Depends(current_user)):
    return build_orbit(db, datetime.now(ZoneInfo(get_settings().crm_timezone)).date())
