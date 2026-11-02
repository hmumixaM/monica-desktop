import json
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text
from test_ai import post
from test_ui_endpoints import person

from app.ai import person_memory, person_weekly
from app.ai.person_context import batches, profile_changes, social
from app.ai.person_weekly import bounds, candidates, previous_week
from app.ai.schemas import PersonWeekAnalysis, PersonWeekPart


def test_week_bounds_use_chicago_calendar_across_fall_dst():
    start, end = bounds(date(2026, 10, 26))
    assert start == datetime(2026, 10, 26, tzinfo=ZoneInfo("America/Chicago"))
    assert end == datetime(2026, 11, 2, tzinfo=ZoneInfo("America/Chicago"))
    assert (end.astimezone(UTC) - start.astimezone(UTC)) == timedelta(hours=169)
    assert previous_week("2026-11-02") == date(2026, 10, 26)


def test_evidence_batches_keep_every_item_without_a_total_cap():
    rows = [{"id": str(n), "text": "朋友圈普通分享" * 50} for n in range(130)]
    groups = batches(rows, max_chars=1000)
    assert len(groups) > 1
    assert [row["id"] for group in groups for row in group] == [row["id"] for row in rows]


def test_oversized_ocr_text_is_split_without_losing_characters():
    original = ("图片中的字\\\"\n" * 400)
    groups = batches([{"id": "p1", "kind": "post", "text": original}], max_chars=1000)
    pieces = [item for group in groups for item in group]
    assert all(len(json.dumps(group, ensure_ascii=False)) <= 1000 for group in groups)
    assert "".join(item["fragment"] for item in pieces) == json.dumps(
        {"id": "p1", "kind": "post", "text": original}, ensure_ascii=False)


def test_old_post_import_is_this_weeks_discovery_and_social_pages_are_complete(crm_db):
    friend = person(crm_db, "分享日常")
    now = datetime.now(UTC)
    start, end = now - timedelta(days=2), now + timedelta(days=2)
    for n in range(65):
        post(crm_db, friend, f"old-sharing-{n:03}", f"普通分享 {n}", days_ago=700)
    rows = social(crm_db, str(friend), start, end)
    assert len(rows) == 65
    assert all(row["discovered"] for row in rows)
    assert str(friend) in candidates(crm_db, start, end)
    # A post from this week is an event, not a historical discovery.
    fresh = post(crm_db, friend, "new-sharing", "本周的普通分享", days_ago=1)
    rows = social(crm_db, str(friend), start, end)
    assert len(rows) == 66
    assert next(row for row in rows if row["post_id"] == fresh)["discovered"] is False
    assert crm_db.execute(text("SELECT count(*) FROM social_posts WHERE profile_id=:id AND ai_seen_at IS NOT NULL"),
                          {"id": friend}).scalar_one() == 66


def test_profile_summary_change_is_a_weekly_source(crm_db):
    friend = person(crm_db, "原名")
    now = datetime.now(UTC)
    start, end = now - timedelta(days=1), now + timedelta(days=1)
    crm_db.execute(text("UPDATE profiles SET display_name='新名字',summary='最近在学摄影' WHERE id=:id"), {"id": friend})
    assert str(friend) in candidates(crm_db, start, end)
    changes = profile_changes(crm_db, str(friend), start, end)
    assert any("新名字" in row["text"] and "最近在学摄影" in row["text"] for row in changes)


def test_weekly_note_uses_memory_and_persists_only_shown_sources(crm_db, monkeypatch):
    friend = person(crm_db, "周末分享")
    today = datetime.now(ZoneInfo("America/Chicago")).date()
    monday = today - timedelta(days=today.weekday())
    post(crm_db, friend, "week-note-old", "去年拍的日常照片", days_ago=400)
    crm_db.execute(text("""INSERT INTO ai_outputs(kind,subject,input_hash,model,output)
        VALUES('profile_memory',:id,:hash,'test',CAST(:output AS jsonb))"""),
        {"id": str(friend), "hash": "a" * 64,
         "output": json.dumps({"summary": "我的老朋友，喜欢分享生活", "facts": [], "gaps": []})})
    monkeypatch.setattr(person_memory, "refresh", lambda *_: False)
    calls = []

    def fake(_model, _system, payload, schema):
        calls.append((schema.__name__, payload))
        assert payload["memory"]["summary"] == "我的老朋友，喜欢分享生活"
        if schema is PersonWeekPart:
            return PersonWeekPart(summary="新补录了一条去年的日常分享", sources=[payload["sources"][0]["id"], "not-shown"])
        return PersonWeekAnalysis(summary="这周新发现了 TA 去年的一条普通分享。", sources=[payload["parts"][0]["sources"][0], "not-shown"])

    monkeypatch.setattr(person_weekly, "structured", fake)
    payload = {"profile_id": str(friend), "week_start": monday.isoformat()}
    assert person_weekly.build_one(crm_db, payload)["changed"] is True
    row = crm_db.execute(text("SELECT output FROM person_weekly_updates WHERE profile_id=:id"), {"id": friend}).scalar_one()
    assert row["summary"] == "这周新发现了 TA 去年的一条普通分享。"
    assert len(row["sources"]) == 1 and row["sources"][0]["discovered"] is True
    assert person_weekly.build_one(crm_db, payload)["changed"] is False
    assert [name for name, _ in calls] == ["PersonWeekPart", "PersonWeekAnalysis"]


def test_memory_evidence_keeps_combined_scene_and_full_ocr(crm_db, tmp_path, monkeypatch):
    from test_image_ocr import enable, setup

    from app.ai import image_ocr, payloads

    enable(monkeypatch)
    friend, pid, _ = setup(crm_db, tmp_path, 1)
    image_ocr.configure(crm_db, enabled=True, daily_limit=100)
    work = image_ocr.claim(crm_db)
    original = '叠字：两只猫见面。' * 150
    image_ocr.finish(crm_db, work, output=original, description='推车旁的两只猫', category='宠物', summary='两只猫见面')
    reading = social(crm_db, str(friend))[0]['media_context']['ocr'][0]
    assert reading['text'] == original
    assert reading['description'] == '推车旁的两只猫'
    assert reading['category'] == '宠物'
    assert len(payloads._visual_context(crm_db, [pid])[pid]['ocr'][0]['text']) <= 601
    crm_db.execute(text("UPDATE image_ocr_readings SET status='pending'"))
    assert social(crm_db, str(friend))[0]['media_context']['ocr'][0] == reading


def test_weekly_people_browses_all_people_with_sources_and_week_filter(crm_db, monkeypatch):
    from uuid import UUID

    from app.config import get_settings

    monkeypatch.setenv("AI_ENABLED", "true")
    get_settings.cache_clear()
    week = date(2026, 10, 5)
    ids = [person(crm_db, f"近况 {n}") for n in range(17)]
    source = {"kind": "story", "id": "story-1", "at": "2026-10-08", "discovered": False, "quote": "一起打球"}
    for pid in ids:
        crm_db.execute(text("""INSERT INTO person_weekly_updates(profile_id,week_start,input_hash,model,output)
            VALUES(:id,:week,:hash,'test',CAST(:output AS jsonb))"""),
            {"id": pid, "week": week, "hash": "a" * 64,
             "output": json.dumps({"summary": "这周分享了运动日常。", "sources": [source]})})
    crm_db.execute(text("UPDATE profiles SET archived_at=now() WHERE id=:id"), {"id": ids[-1]})
    crm_db.execute(text("UPDATE profiles SET profile_type='group' WHERE id=:id"), {"id": ids[-2]})
    crm_db.execute(text("""INSERT INTO person_weekly_updates(profile_id,week_start,input_hash,model,output)
        VALUES(:id,'2026-09-28',:hash,'test',CAST(:output AS jsonb))"""),
        {"id": ids[0], "hash": "b" * 64, "output": json.dumps({"summary": "上周近况", "sources": []})})
    first = person_weekly.weekly_people(week=week, after=None, limit=12, db=crm_db)
    assert first["total"] == 15 and len(first["people"]) == 12
    assert first["weeks"] == [week, date(2026, 9, 28)]
    assert first["people"][0]["sources"] == [source]
    second = person_weekly.weekly_people(week=week, after=UUID(first["next_cursor"]), limit=12, db=crm_db)
    all_ids = [row["profile_id"] for row in first["people"] + second["people"]]
    assert len(second["people"]) == 3 and second["next_cursor"] is None
    assert len(set(all_ids)) == 15
    assert set(all_ids) == {str(pid) for pid in ids[:-2]}
    old = person_weekly.weekly_people(week=date(2026, 9, 28), after=None, limit=12, db=crm_db)
    assert old["total"] == 1 and old["people"][0]["summary"] == "上周近况"
    empty = person_weekly.weekly_people(week=date(2026, 9, 21), after=None, limit=12, db=crm_db)
    assert empty["total"] == 0 and empty["people"] == [] and empty["next_cursor"] is None


def test_weekly_people_disabled_has_no_database_or_model_calls(monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("AI_ENABLED", "false")
    get_settings.cache_clear()
    assert person_weekly.weekly_people(db=None)["enabled"] is False
    get_settings.cache_clear()
