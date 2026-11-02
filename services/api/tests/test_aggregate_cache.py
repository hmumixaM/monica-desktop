from decimal import Decimal

from app import aggregate_cache, location_atlas, social_analytics


class Versions:
    def __init__(self):
        self.value = {"posts": 1}

    def __call__(self, db, domains):
        return {domain: self.value.get(domain, 0) for domain in sorted(domains)}


def test_aggregate_cache_survives_process_memory_and_follows_versions(monkeypatch) -> None:
    versions = Versions()
    monkeypatch.setattr(aggregate_cache, "current", versions)
    calls = []

    def compute(_db):
        calls.append(1)
        return {"points": [{"label": "Chicago"}], "count": Decimal(3), "n": len(calls)}

    first = aggregate_cache.cached_record(None, "location-atlas", ("posts",), ["k"], compute)
    aggregate_cache.clear_memory()
    second = aggregate_cache.cached_record(None, "location-atlas", ("posts",), ["k"], compute)
    assert second == first and first["value"]["count"] == 3 and len(calls) == 1
    versions.value["posts"] = 2
    third = aggregate_cache.cached_record(None, "location-atlas", ("posts",), ["k"], compute)
    assert third["value"]["n"] == 2 and third["etag"] != first["etag"]
    assert location_atlas.CACHE_SECONDS == 604800
    assert social_analytics.CACHE_SECONDS == 604800


def test_expired_aggregate_is_served_stale_and_refreshed_in_background(monkeypatch) -> None:
    monkeypatch.setattr(aggregate_cache, "current", Versions())
    refreshed = []
    monkeypatch.setattr(aggregate_cache, "_refresh_later", lambda *args: refreshed.append(args[1]))
    record = aggregate_cache.cached_record(None, "home", ("posts",), "all", lambda _db: {"v": 1}, ttl=60)
    record["computed_at"] -= 120
    again = aggregate_cache.cached_record(None, "home", ("posts",), "all", lambda _db: {"v": 2}, ttl=60)
    assert again["value"] == {"v": 1} and len(refreshed) == 1


def test_heavy_aggregates_serve_previous_value_while_recomputing(monkeypatch) -> None:
    versions = Versions()
    monkeypatch.setattr(aggregate_cache, "current", versions)
    refreshed = []
    monkeypatch.setattr(aggregate_cache, "_refresh_later", lambda *args: refreshed.append(args[1]))
    aggregate_cache.cached_record(None, "metrics-summary", ("posts",), "all", lambda _db: {"v": 1})
    versions.value["posts"] = 2
    stale = aggregate_cache.cached_record(None, "metrics-summary", ("posts",), "all", lambda _db: {"v": 2},
                                          stale_on_change=True)
    assert stale["value"] == {"v": 1} and len(refreshed) == 1
    fresh = aggregate_cache.cached_record(None, "metrics-summary", ("posts",), "all", lambda _db: {"v": 2})
    assert fresh["value"] == {"v": 2}


def test_import_only_changes_serve_stale_but_user_edits_recompute(monkeypatch) -> None:
    versions = Versions()
    versions.value = {"posts": 1, "profiles": 1}
    monkeypatch.setattr(aggregate_cache, "current", versions)
    refreshed = []
    monkeypatch.setattr(aggregate_cache, "_refresh_later", lambda *args: refreshed.append(args[1]))
    domains = ("posts", "profiles")
    aggregate_cache.cached_record(None, "overview", domains, "p", lambda _db: {"v": 1})
    versions.value["posts"] = 2
    imported = aggregate_cache.cached_record(None, "overview", domains, "p", lambda _db: {"v": 2},
                                             stale_domains=aggregate_cache.IMPORT_DOMAINS)
    assert imported["value"] == {"v": 1} and len(refreshed) == 1
    versions.value["profiles"] = 2
    edited = aggregate_cache.cached_record(None, "overview", domains, "p", lambda _db: {"v": 3},
                                           stale_domains=aggregate_cache.IMPORT_DOMAINS)
    assert edited["value"] == {"v": 3}



def test_an_endpoint_whose_source_file_changed_does_not_read_old_entries(monkeypatch) -> None:
    from app import home, insights

    monkeypatch.setattr(aggregate_cache, "current", Versions())
    old = aggregate_cache.cached_record(None, "shape", ("posts",), "all", lambda _db: {"v": 1}, source=home.home)
    same = aggregate_cache.cached_record(None, "shape", ("posts",), "all", lambda _db: {"v": 2}, source=home.home)
    changed = aggregate_cache.cached_record(None, "shape", ("posts",), "all", lambda _db: {"v": 3}, source=insights.insights)
    assert old["value"] == same["value"] == {"v": 1} and changed["value"] == {"v": 3}
