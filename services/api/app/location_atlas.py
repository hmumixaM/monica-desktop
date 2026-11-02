"""Global location metadata, conservative offline city resolution, bounded detail."""

import os
import re
from collections import defaultdict
from functools import lru_cache

from sqlalchemy import text

from .aggregate_cache import cached_aggregate
from .city_reference import administrative_names, cities_named, normalized


@lru_cache(maxsize=4096)
def resolve_city(label, country="", region=""):
    countries, states = administrative_names()
    parts = [p.strip() for p in re.split(r"[,，·]", str(label)) if p.strip()]
    code = countries.get(normalized(country)) or next(
        (countries[normalized(p)] for p in reversed(parts) if normalized(p) in countries), None
    )
    state = states.get(normalized(region)) or next(
        (states[normalized(p)] for p in parts if normalized(p) in states), None
    )
    if state and not code:
        code = "US"
    for part in parts:
        key = normalized(part)
        candidates = cities_named(key)
        if not candidates and key.endswith("市"):
            candidates = cities_named(key[:-1])
        matches = [
            city
            for city in candidates
            if (not code or city["countrycode"] == code)
            and (not state or city["admin1code"] == state)
        ]
        if len(matches) == 1:
            c = matches[0]
            return {
                "latitude": c["latitude"],
                "longitude": c["longitude"],
                "label": f"{c['name']} · {c['countrycode']}",
                "geoname_id": c["geonameid"],
                "precision": "city",
            }
        if matches:
            return None  # Never choose the largest of ambiguous names.
    return None


CACHE_SECONDS = max(int(os.environ.get("LOCATION_ATLAS_CACHE_SECONDS", "604800")), 90)


def atlas(db, provider=None, year=None, kind="all"):
    return cached_aggregate(
        db, "location-atlas-v2", ("posts", "links", "profiles"), [provider, year, kind],
        lambda session: compute_atlas(session, provider, year, kind), ttl=CACHE_SECONDS,
    )


def compute_atlas(db, provider=None, year=None, kind="all"):
    from .location_points import _location_label, _location_point

    params = {"provider": provider, "year": int(year) if year else None}
    entries = []
    unresolved = defaultdict(lambda: {"people": {}, "sources": set()})
    source_counts = defaultdict(int)
    years = []
    if kind != "profile":
        years = (
            db.execute(
                text("""SELECT DISTINCT extract(year FROM occurred_at)::int AS year FROM social_location_events
            WHERE occurred_at IS NOT NULL AND (CAST(:provider AS text) IS NULL OR provider=:provider) ORDER BY year DESC"""),
                params,
            )
            .scalars()
            .all()
        )
        rows = db.execute(
            text("""SELECT p.id,p.profile_id,person.display_name,p.provider,p.occurred_at,
            p.location,p.coordinate_format,p.event_kind
            FROM social_location_events p
            JOIN profiles person ON person.id=p.profile_id AND person.profile_type='person' AND person.archived_at IS NULL
            WHERE p.location IS NOT NULL AND p.location<>'null'::jsonb
                AND NOT p.location @> '{"latitude":"0","longitude":"0"}'::jsonb
            AND (CAST(:provider AS text) IS NULL OR p.provider=:provider)
            AND (CAST(:year AS int) IS NULL OR (p.occurred_at>=make_date(:year,1,1)::timestamptz
                AND p.occurred_at<make_date(:year+1,1,1)::timestamptz))"""),
            params,
        ).mappings()
        for row in rows:
            point = _location_point(row["location"], row["coordinate_format"])
            if not point:
                continue
            source_counts[row["provider"]] += 1
            entries.append(
                {
                    "point": point,
                    "label": _location_label(row["location"]),
                    "profile_id": str(row["profile_id"]),
                    "name": row["display_name"]
                    + (" · Story 地点标签" if row["event_kind"] == "story" else ""),
                    "provider": row["provider"],
                    "kind": "post",
                    "at": row["occurred_at"].isoformat() if row["occurred_at"] else "",
                    "url": f"/profiles/{row['profile_id']}?tab=timeline&event={row['id']}",
                    "precision": "source",
                }
            )
    if kind != "post":
        rows = db.execute(
            text("""SELECT c.profile_id,p.display_name,c.field_key,c.value
            FROM profile_metric_values c JOIN profiles p ON p.id=c.profile_id
            WHERE p.profile_type='person' AND p.archived_at IS NULL AND c.field_key IN
            ('wechat.city','wechat.province','wechat.country','wechat.region','linkedin.location','instagram.location')""")
        ).mappings()
        fields = defaultdict(dict)
        names = {}
        for row in rows:
            fields[str(row["profile_id"])][row["field_key"]] = row["value"]
            names[str(row["profile_id"])] = row["display_name"]
        for pid, values in fields.items():
            for source in ("wechat", "linkedin", "instagram"):
                if provider and provider != source:
                    continue
                country = values.get("wechat.country", "") if source == "wechat" else ""
                region = values.get("wechat.province", "") if source == "wechat" else ""
                label = (
                    (values.get("wechat.city") or values.get("wechat.region"))
                    if source == "wechat"
                    else values.get(source + ".location")
                )
                if not label:
                    continue
                city = resolve_city(str(label), str(country), str(region))
                if not city:
                    title = " · ".join(str(x) for x in (country, region, label) if x)
                    unresolved[title]["people"][pid] = names[pid]
                    unresolved[title]["sources"].add(source)
                    continue
                source_counts[source] += 1
                entries.append(
                    {
                        "point": (city["latitude"], city["longitude"]),
                        "label": city["label"],
                        "profile_id": pid,
                        "name": names[pid],
                        "provider": source,
                        "kind": "profile",
                        "at": "",
                        "url": f"/profiles/{pid}",
                        "precision": "city",
                    }
                )
    # A map bucket's title and event preview must not change with the SQL
    # access path. Use the most recent real observation as its representative.
    entries.sort(key=lambda e: (e["at"], e["url"]), reverse=True)
    groups = {}
    people = set()
    for e in entries:
        lat, lon = e["point"]
        key = (round(lat, 3), round(lon, 3), e["kind"])
        people.add(e["profile_id"])
        g = groups.setdefault(
            key,
            {
                "label": e["label"],
                "latitude": lat,
                "longitude": lon,
                "count": 0,
                "sources": set(),
                "people": {},
                "events": [],
                "months": defaultdict(int),
                "source_kind": e["kind"],
                "precision": e["precision"],
            },
        )
        g["count"] += 1
        g["sources"].add(e["provider"])
        g["people"][e["profile_id"]] = e["name"]
        if e["at"]:
            g["months"][e["at"][:7]] += 1
        if len(g["events"]) < 8:
            g["events"].append(
                {"at": e["at"], "text": e["name"], "source": e["provider"], "url": e["url"]}
            )
    points = []
    for g in groups.values():
        g["sources"] = sorted(g["sources"])
        g["months"] = dict(sorted(g["months"].items()))
        g["people_count"] = len(g["people"])
        g["people"] = [{"id": pid, "name": name} for pid, name in g["people"].items()]
        points.append(g)
    unresolved_rows = [
        {
            "label": label,
            "count": len(g["people"]),
            "sources": sorted(g["sources"]),
            "people": [{"id": pid, "name": name} for pid, name in g["people"].items()],
        }
        for label, g in unresolved.items()
    ]
    result = {
        "points": sorted(points, key=lambda p: -p["count"]),
        "people": len(people),
        "place_count": len(points),
        "located_records": len(entries),
        "post_records": sum(e["kind"] == "post" for e in entries),
        "profile_records": sum(e["kind"] == "profile" for e in entries),
        "source_counts": dict(source_counts),
        "years": years,
        "months": sorted({month for point in points for month in point["months"]}),
        "unresolved": sorted(unresolved_rows, key=lambda r: -r["count"]),
        "city_reference": "GeoNames cities15000; exact names and country/state constraints; city centers only",
    }
    return result
