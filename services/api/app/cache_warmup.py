"""Precompute global aggregates after an import so first page loads are hot."""

import inspect
import json
import time

from .db import SessionLocal


def targets():
    from . import query_services
    from .aggregate_cache import cached_aggregate
    from .data_versions import DOMAINS

    def query(name, domains, func, db):
        key = {k: v.default for k, v in inspect.signature(func).parameters.items() if k != "db"}
        return cached_aggregate(db, name, domains, key, lambda session: func(db=session), source=func)
    from .directory import directory_snapshot
    from .home import home
    from .insights import insights
    from .location_atlas import atlas
    from .social_analytics import social_analytics

    return {
        "home": lambda db: home(db=db, _user=None),
        **{f"insights-{days}": (lambda db, days=days: insights(dormant_days=days, db=db, _user=None)) for days in (90, 180, 365)},
        "directory": lambda db: directory_snapshot(db=db, _user=None),
        "metrics-summary": lambda db: query("metrics-summary", DOMAINS, query_services.metrics_summary, db),
        "people-overview": lambda db: query("people-overview", DOMAINS, query_services.people_overview, db),
        "tags": lambda db: query("tags", ("tags", "profiles"), query_services.list_tags, db),
        "relationship-graph": lambda db: query("relationship-graph", ("profiles", "links", "posts", "media", "ai", "faces"), query_services.relationship_graph, db),
        "social-analytics": lambda db: social_analytics(db),
        "location-atlas": lambda db: atlas(db),
    }


def warm(session_factory=SessionLocal, names=None) -> dict:
    report = {}
    for name, compute in targets().items():
        if names and name not in names:
            continue
        started = time.monotonic()
        with session_factory() as db:
            compute(db)
        report[name] = round(time.monotonic() - started, 2)
    return report


if __name__ == "__main__":
    print(json.dumps({"cache_warmup_seconds": warm()}), flush=True)
