import ast
import json
import re
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from random import Random
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import event, text
from test_ui_endpoints import person

from app.ai import payloads, reconnect
from app.ai.digest import _valid, previous_week
from app.ai.reconnect import ReconnectAction, act_on_reconnect, shuffled, undo_reconnect, weight
from app.ai.schedule import SCHEDULES, enqueue_due
from app.ai.schemas import CommunityName, DigestNarrative, Reconnect
from app.ai.store import generate
from app.config import get_settings
from app.contact_stats import refresh

SECRET = "这句聊天内容绝不能发给模型"
CHAT_TABLES = ("message_events", "message_search", "conversations", "content_objects")


def heavy_chat(db, pid, external, months_ago: int, count: int):
    """`count` direct messages, all with SECRET as their body, ending `months_ago` months back."""
    conversation = db.execute(text("""INSERT INTO conversations(conversation_type,profile_id,external_id)
        VALUES('direct',:p,:e) RETURNING id"""), {"p": pid, "e": "direct:" + external}).scalar_one()
    db.execute(text("""WITH sent AS (
          INSERT INTO message_events(conversation_id,sender_profile_id,occurred_at,message_type,source_record_id)
          SELECT :c, :p, now() - make_interval(days => :days, mins => n), '1', :e || n FROM generate_series(1, :count) AS n
          RETURNING id, occurred_at)
        INSERT INTO message_search(message_id,occurred_at,conversation_id,body) SELECT id, occurred_at, :c, :body FROM sent"""),
               {"c": conversation, "p": pid, "days": months_ago * 30, "e": external, "count": count, "body": SECRET})
    refresh(db)


def post(db, pid, external, caption, days_ago=3):
    content = db.execute(text("""INSERT INTO content_objects(sha256,content_kind,byte_length,storage_uri,payload_json)
        VALUES(:sha,'post',1,'x',CAST(:body AS jsonb)) RETURNING id"""),
        {"sha": external.ljust(64, "0"), "body": json.dumps({"caption": caption})}).scalar_one()
    return str(db.execute(text("""INSERT INTO social_posts(provider,external_id,profile_id,occurred_at,current_content_hash,content_object_id)
        VALUES('wechat',:e,:p,now() - make_interval(days => :days),:h,:c) RETURNING id"""),
        {"e": external, "p": pid, "days": days_ago, "h": "h" * 64, "c": content}).scalar_one())


def test_payload_sql_never_names_a_chat_table():
    source = Path(payloads.__file__).read_text()
    tree = ast.parse(source)
    strings = [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)]
    sql = [value for value in strings if "SELECT" in value]
    assert sql, "payload SQL should be found"
    for statement in sql:
        assert not payloads.FORBIDDEN.search(statement), statement
    # `read` is the only way payloads reach the database.
    assert source.count("db.execute(") == 1


def test_read_refuses_chat_tables():
    for table in CHAT_TABLES:
        with pytest.raises(PermissionError):
            payloads.read(None, f"SELECT * FROM {table}", {})


def test_payloads_carry_no_chat_text_and_issue_no_chat_queries(crm_db):
    friend = person(crm_db, "老同学")
    heavy_chat(crm_db, friend, "wxid_old", months_ago=6, count=260)
    post_id = post(crm_db, friend, "moment-1", "搬到西雅图了，新工作第一天")
    statements = []
    connection = crm_db.connection()
    listener = lambda _conn, _cursor, statement, *_args: statements.append(statement)
    event.listen(connection, "before_cursor_execute", listener)
    since = datetime.now(UTC) - timedelta(days=90)
    built = [
        payloads.reconnect_payload(crm_db, str(friend), datetime.now(UTC).date(), since),
        payloads.community_payload(crm_db, [str(friend)]),
        payloads.reconnect_candidates(crm_db, 200, 120, 90),
        payloads.week_captions(crm_db, since, datetime.now(UTC), 10),
    ]
    event.remove(connection, "before_cursor_execute", listener)

    encoded = json.dumps(built, ensure_ascii=False, default=str)
    assert SECRET not in encoded
    assert built[0]["recent_posts"][0] | {"date": None} == {
        "post_id": post_id, "profile_id": str(friend), "provider": "wechat", "date": None, "caption": "搬到西雅图了，新工作第一天"}
    assert [row["post_id"] for row in built[3]] == [post_id]
    # Contact context reads only current profile details; network context reads aggregate social counts.
    assert statements and not any(re.search(r"\b(message_events|message_search|messages|conversations)\b", statement) for statement in statements)
    assert built[0]["history"]["messages_total"] == 260
    assert [row["profile_id"] for row in built[2]] == [str(friend)]


def test_generate_skips_the_model_when_its_input_is_unchanged(crm_db):
    calls = []

    def fake(model, system, payload, schema):
        calls.append(payload)
        return CommunityName(label=f"第{len(calls)}版", reason="测试")

    assert generate(crm_db, "community_name", "c:x", "m", "s", {"tags": ["a"]}, CommunityName, fake)
    assert not generate(crm_db, "community_name", "c:x", "m", "s", {"tags": ["a"]}, CommunityName, fake)
    assert generate(crm_db, "community_name", "c:x", "m", "s", {"tags": ["b"]}, CommunityName, fake)
    label = crm_db.execute(text("SELECT output->>'label' FROM ai_outputs WHERE subject = 'c:x'")).scalar_one()
    assert len(calls) == 2 and label == "第2版"


def test_schedules_run_once_per_period_after_their_local_time(crm_db, monkeypatch):
    zone = ZoneInfo("America/Chicago")
    daily, weekly = SCHEDULES[0], SCHEDULES[2]
    assert daily.period(datetime(2026, 10, 13, 2, 59, tzinfo=zone)) is None
    assert daily.period(datetime(2026, 10, 13, 3, 0, tzinfo=zone)) == "2026-10-13"
    assert weekly.period(datetime(2026, 10, 12, 7, 59, tzinfo=zone)) is None
    assert weekly.period(datetime(2026, 10, 12, 8, 0, tzinfo=zone)) == "2026-10-12"
    assert weekly.period(datetime(2026, 10, 18, 23, 0, tzinfo=zone)) == "2026-10-12"
    assert previous_week("2026-10-12") == date(2026, 10, 5)

    monkeypatch.setenv("AI_ENABLED", "false")
    get_settings.cache_clear()
    now = datetime(2026, 10, 13, 12, 0, tzinfo=zone)
    assert enqueue_due(crm_db, now) == []
    monkeypatch.setenv("AI_ENABLED", "true")
    get_settings.cache_clear()
    assert sorted(enqueue_due(crm_db, now)) == ["ai.community_names", "ai.digest", "ai.person_weeks", "ai.profiles", "ai.reconnect"]
    assert enqueue_due(crm_db, now + timedelta(hours=1)) == []
    jobs = crm_db.execute(text("SELECT job_type, payload->>'period' FROM jobs ORDER BY job_type")).all()
    assert jobs == [("ai.community_names", "2026-10-13"), ("ai.digest", "2026-10-12"),
                    ("ai.person_weeks", "2026-10-12"), ("ai.profiles", "2026-10-11"),
                    ("ai.reconnect", "2026-10-13")]
    get_settings.cache_clear()


def test_reconnect_states_hide_suggestions_until_they_lapse(crm_db):
    friend = person(crm_db, "老朋友")
    heavy_chat(crm_db, friend, "wxid_friend", months_ago=6, count=220)
    shown = lambda: [row["profile_id"] for row in payloads.reconnect_candidates(crm_db, 200, 120, 90)]
    assert shown() == [str(friend)]

    act_on_reconnect(friend, ReconnectAction(state="snoozed", snooze="2w"), crm_db)
    assert shown() == []
    undo_reconnect(friend, crm_db)
    assert shown() == [str(friend)]
    act_on_reconnect(friend, ReconnectAction(state="done"), crm_db)
    assert shown() == []
    crm_db.execute(text("UPDATE reconnect_states SET updated_at = now() - interval '181 days'"))
    assert shown() == [str(friend)]
    act_on_reconnect(friend, ReconnectAction(state="dismissed"), crm_db)
    crm_db.execute(text("UPDATE reconnect_states SET updated_at = now() - interval '400 days'"))
    assert shown() == []


def test_reconnect_shuffle_favours_close_overdue_people_but_reaches_everyone():
    now = datetime.now(UTC)
    quiet = lambda days, peak=400, posts=0: {"profile_id": f"{peak}-{days}-{posts}", "peak": peak,
                                              "last_contact_at": now - timedelta(days=days), "recent_posts": posts}
    close_year, close_month, far_year, lost, posting = (quiet(365), quiet(60), quiet(365, peak=100), quiet(365 * 9),
                                                        quiet(365, posts=2))
    assert weight(close_year, now) > weight(close_month, now)
    assert weight(close_year, now) > weight(far_year, now)
    assert weight(close_year, now) > weight(lost, now) >= 0.4 * weight(close_year, now)
    assert weight(posting, now) == 1.5 * weight(close_year, now)

    rng = Random(7)
    people = [close_year, close_month, far_year, lost]
    firsts = Counter(shuffled(people, now, rng)[0]["profile_id"] for _ in range(2000))
    assert set(firsts) == {person["profile_id"] for person in people}
    assert firsts.most_common(1)[0][0] == close_year["profile_id"]


def test_reconnect_queues_one_job_per_person_missing_a_reason(crm_db, monkeypatch):
    first, second = person(crm_db, "老同事"), person(crm_db, "老室友")
    heavy_chat(crm_db, first, "wxid_first", months_ago=6, count=220)
    heavy_chat(crm_db, second, "wxid_second", months_ago=8, count=150)
    monkeypatch.setenv("AI_ENABLED", "true")
    get_settings.cache_clear()
    assert reconnect.refresh(crm_db, {})["queued"] == 2
    assert reconnect.refresh(crm_db, {})["queued"] == 0

    monkeypatch.setattr(reconnect, "structured", lambda *_args: Reconnect(reason="以前常聊", opener="最近怎么样？", sources=[]))
    for pid in (first, second):
        assert reconnect.refresh_one(crm_db, {"profile_id": str(pid)})["generated"]
    crm_db.execute(text("UPDATE jobs SET status = 'complete' WHERE job_type = 'ai.reconnect_one'"))
    assert reconnect.refresh(crm_db, {}) | {"candidates": None} == {"candidates": None, "missing": 0, "stale": 0, "queued": 0}
    shown = reconnect.list_reconnect(crm_db)["people"]
    assert {row["profile_id"] for row in shown} == {str(first), str(second)}
    assert all(row["suggestion"]["opener"] == "最近怎么样？" for row in shown)
    get_settings.cache_clear()


def test_nested_lists_returned_as_json_text_are_decoded():
    narrative = DigestNarrative.model_validate_json(json.dumps({
        "summary": "一周", "suggestions": "[]",
        "life_events": json.dumps([{"profile_id": "p", "kind": "move", "text": "搬家", "post_id": "x"}]),
    }))
    assert narrative.life_events[0].kind == "move" and narrative.suggestions == []


def test_digest_drops_events_and_suggestions_the_model_was_not_shown():
    payload = {"facts": {"top_contacts": [{"profile_id": "p1"}], "new_people": [], "new_links": [], "upcoming_dates": [],
                         "newly_dormant": []},
               "posts": [{"post_id": "post1", "profile_id": "p2"}]}
    narrative = DigestNarrative(summary="一周", life_events=[
        {"profile_id": "p2", "kind": "move", "text": "搬家", "post_id": "post1"},
        {"profile_id": "p1", "kind": "travel", "text": "编的", "post_id": "post1"},
        {"profile_id": "p2", "kind": "travel", "text": "编的", "post_id": "post9"},
    ], suggestions=[{"profile_id": "p1", "reason": "常联系"}, {"profile_id": "p9", "reason": "编的"}])
    result = _valid(narrative, payload)
    assert [event["text"] for event in result["life_events"]] == ["搬家"]
    assert [item["profile_id"] for item in result["suggestions"]] == ["p1"]
