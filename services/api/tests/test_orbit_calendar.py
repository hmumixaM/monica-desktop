from datetime import UTC, date, datetime, timedelta

from sqlalchemy import text
from test_ui_endpoints import chat, person

from app.contact_calendar import day_people, year_counts
from app.contact_stats import refresh
from app.orbit import RINGS, build_orbit, months_back, place, ring_of


def test_months_back_crosses_year_boundaries():
    assert months_back(date(2026, 2, 14), 3) == date(2025, 12, 1)
    assert months_back(date(2026, 10, 13), 12) == date(2025, 11, 1)
    assert months_back(date(2026, 1, 31), 1) == date(2026, 1, 1)


def test_rings_follow_dunbar_layers():
    assert [ring_of(rank) for rank in (0, 4, 5, 14, 15, 49, 50, 149, 150)] == [0, 0, 1, 1, 2, 2, 3, 3, None]


def test_place_ranks_each_window_and_keeps_anyone_inside_either():
    rows = [{"id": n, "name": f"p{n:03}", "score_short": 0, "score_long": 200 - n} for n in range(160)]
    rows.append({"id": "recent", "name": "recent", "score_short": 50, "score_long": 1})
    rings = place(rows)
    assert rings["recent"] == {3: 0}
    assert rings["0"] == {12: 0} and rings["5"] == {12: 1}
    assert "155" not in rings and len(rings) == RINGS[-1] + 1


def test_orbit_groups_people_by_shared_small_groups(crm_db):
    now = datetime.now(UTC)
    friends = [person(crm_db, f"Friend {n}") for n in range(4)]
    loner = person(crm_db, "Loner")
    for n, pid in enumerate([*friends, loner]):
        chat(crm_db, pid, f"wxid_orbit_{n}", [(now - timedelta(days=d), d % 2 == 0, "x") for d in range(1, 12 - n)])
    group = person(crm_db, "Climbing", kind="group")
    for pid in friends:
        crm_db.execute(text("INSERT INTO group_memberships(group_profile_id,person_profile_id) VALUES(:g,:p)"), {"g": group, "p": pid})
    refresh(crm_db)

    result = build_orbit(crm_db, now.date())

    assert result["rings"] == list(RINGS)
    by_id = {person["profile_id"]: person for person in result["people"]}
    assert set(by_id) == {str(pid) for pid in [*friends, loner]}
    assert {by_id[str(pid)]["community"] for pid in friends} == {f"g:{group}"}
    assert by_id[str(loner)]["community"] == "other"
    communities = {community["id"]: community for community in result["communities"]}
    assert communities[f"g:{group}"] == {"id": f"g:{group}", "label": "Climbing", "size": 4}
    assert by_id[str(friends[0])]["ring"] == {"3": 0, "12": 0}
    assert by_id[str(friends[0])]["messages"]["12"] == 11


def test_calendar_counts_distinct_people_per_day(crm_db):
    now = datetime.now(UTC).replace(hour=17, minute=0, second=0, microsecond=0)
    a, b = person(crm_db, "Alice"), person(crm_db, "Bob")
    chat(crm_db, a, "wxid_cal_a", [(now, False, "hi"), (now + timedelta(minutes=5), True, "hey")])
    chat(crm_db, b, "wxid_cal_b", [(now, False, "yo")])
    refresh(crm_db)
    zone = "UTC"

    result = year_counts(crm_db, now.year, zone)

    assert {"date": now.date(), "people": 2} in result["days"]
    assert result["max"] == 2 and now.year in result["years"]
    people = day_people(crm_db, now.date(), zone)
    assert [(entry["name"], entry["messages"], entry["calls"]) for entry in people] == [("Alice", 2, 0), ("Bob", 1, 0)]
    assert "body" not in people[0]
