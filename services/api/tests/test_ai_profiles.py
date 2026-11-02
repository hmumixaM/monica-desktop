import ast
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import event, text
from test_ai import SECRET, heavy_chat, post
from test_ui_endpoints import chat, person

from app.ai import person_memory, profiles
from app.ai.payloads import overview_candidates, overview_payload
from app.ai.schemas import MemoryPart, ProfileOverview, ProfileUpdate, ProfileUpdates
from app.config import get_settings

APP = Path(profiles.__file__).parents[1]
CHAT_SQL = re.compile(r"\b(message_events|message_search|conversations)\b")


def tags(db, pid, *names):
    for name in names:
        tag = db.execute(text("INSERT INTO tags(name) VALUES(:n) RETURNING id"), {"n": name}).scalar_one()
        db.execute(text("INSERT INTO tag_memberships(tag_id,profile_id,source_type) VALUES(:t,:p,'manual')"), {"t": tag, "p": pid})


def group_line(db, pid, group_name, body, at):
    group = person(db, group_name, kind="group")
    conversation = db.execute(text("""INSERT INTO conversations(conversation_type,profile_id,external_id)
        VALUES('group',:g,:e) RETURNING id"""), {"g": group, "e": f"group:{group}"}).scalar_one()
    message = db.execute(text("""INSERT INTO message_events(conversation_id,sender_profile_id,occurred_at,message_type,source_record_id)
        VALUES(:c,:p,:at,'1','g1') RETURNING id"""), {"c": conversation, "p": pid, "at": at}).scalar_one()
    db.execute(text("INSERT INTO message_search(message_id,occurred_at,conversation_id,body) VALUES(:m,:at,:c,:b)"),
               {"m": message, "at": at, "c": conversation, "b": body})


def today():
    return datetime.now(ZoneInfo(get_settings().crm_timezone)).date()


def test_only_person_memory_and_profile_suggestions_read_chat_text():
    importers = sorted(path.relative_to(APP).as_posix() for path in APP.rglob("*.py")
                       for node in ast.walk(ast.parse(path.read_text()))
                       if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("chat_excerpts"))
    assert importers == ["ai/person_context.py", "ai/profiles.py"]


def test_overviews_need_enough_information_and_carry_no_chat_text(crm_db):
    rich, thin, quiet = person(crm_db, "资料多"), person(crm_db, "资料少"), person(crm_db, "不发动态")
    tags(crm_db, rich, "大学同学", "上海", "摄影")
    tags(crm_db, thin, "同事", "北京")
    tags(crm_db, quiet, "邻居", "跑步", "狗")
    for n in range(5):
        post(crm_db, rich, f"rich-{n}", f"第 {n} 条动态")
        post(crm_db, thin, f"thin-{n}", f"第 {n} 条")
    heavy_chat(crm_db, rich, "wxid_rich", months_ago=1, count=30)
    assert overview_candidates(crm_db) == [str(rich)]
    heavy_chat(crm_db, quiet, "wxid_quiet", months_ago=1, count=120)
    assert set(overview_candidates(crm_db)) == {str(rich), str(quiet)}

    statements = []
    connection = crm_db.connection()
    listener = lambda _conn, _cursor, statement, *_args: statements.append(statement)
    event.listen(connection, "before_cursor_execute", listener)
    payload = overview_payload(crm_db, str(rich))
    event.remove(connection, "before_cursor_execute", listener)
    assert SECRET not in json.dumps(payload, ensure_ascii=False, default=str)
    assert statements and not any(CHAT_SQL.search(statement) for statement in statements)
    assert sorted(payload["person"]["tags"]) == sorted(["大学同学", "上海", "摄影"])
    assert len(payload["posts"]) == 5 and payload["history"]["messages_total"] == 30


def test_update_payload_labels_speakers_and_keeps_only_shown_evidence(crm_db):
    friend = person(crm_db, "老友")
    now = datetime.now(UTC)
    chat(crm_db, friend, "wxid_laoyou", [(now - timedelta(days=200), False, "太久以前的话"),
                                         (now - timedelta(days=3), False, "我搬到杭州了"),
                                         (now - timedelta(days=2), True, "周六一起吃饭？")])
    group_line(crm_db, friend, "读书会", "这周我在杭州", now - timedelta(days=1))
    day = today()
    payload, refs = profiles.updates_payload(crm_db, str(friend), {"summary": "老同学", "facts": [], "gaps": ["生日"]}, day)
    assert [(line["speaker"], line["text"]) for line in payload["direct_chat"]] == [("TA", "我搬到杭州了"), ("我", "周六一起吃饭？")]
    assert [(line["group"], line["text"]) for line in payload["group_chat"]] == [("读书会", "这周我在杭州")]
    assert refs["d1"]["kind"] == "direct" and refs["g1"]["kind"] == "group"

    soon = (day + timedelta(days=4)).isoformat()
    updates = ProfileUpdates(items=[
        ProfileUpdate(kind="residence", value="杭州", evidence=["d1", "g1"], quote="我搬到杭州了", confidence="high"),
        ProfileUpdate(kind="company", value="编的公司", evidence=["d9"], quote="不存在", confidence="high"),
        ProfileUpdate(kind="hometown", value="上海", evidence=["d1"], quote="已知", confidence="medium"),
        ProfileUpdate(kind="birthday", value="02-30", evidence=["d1"], quote="不存在的日子", confidence="high"),
        ProfileUpdate(kind="meetup", value="一起吃饭", date=soon, evidence=["d2"], quote="周六一起吃饭？", confidence="high"),
        ProfileUpdate(kind="meetup", value="很久以后", date=(day + timedelta(days=200)).isoformat(), evidence=["d2"], quote="远",
                      confidence="high"),
        ProfileUpdate(kind="residence", value=" 杭州 ", evidence=["d1"], quote="重复", confidence="high"),
    ])
    resolved = profiles.resolve(updates, refs, {"hometown": "上海"}, day).items
    assert [(item["kind"], item["value"], item["date"]) for item in resolved] == [("residence", "杭州", None), ("meetup", "一起吃饭", soon)]
    assert [ref["text"] for ref in resolved[0]["evidence"]] == ["我搬到杭州了", "这周我在杭州"]


def test_refresh_reuses_unchanged_input_and_decisions_write_the_profile(crm_db, monkeypatch):
    friend = person(crm_db, "小李")
    user = {"id": crm_db.execute(text("INSERT INTO app_users(email,password_hash) VALUES('ai@example.test','x') RETURNING id")).scalar_one()}
    now = datetime.now(UTC)
    chat(crm_db, friend, "wxid_xiaoli", [(now - timedelta(days=5), False, "我生日是 3 月 14 日，现在住在杭州"),
                                         (now - timedelta(days=4), True, "那下周三公司楼下见")])
    soon = (today() + timedelta(days=2)).isoformat()
    calls = []

    def fake(model, system, payload, schema):
        calls.append(schema.__name__)
        if schema is MemoryPart:
            return MemoryPart(summary="大学同学，住在杭州", facts=["住在杭州"])
        if schema is ProfileOverview:
            return ProfileOverview(summary="大学同学", facts=[], gaps=["生日"])
        return ProfileUpdates(items=[
            ProfileUpdate(kind="birthday", value="03-14", evidence=["d1"], quote="我生日是 3 月 14 日", confidence="high"),
            ProfileUpdate(kind="residence", value="杭州", evidence=["d1"], quote="现在住在杭州", confidence="high"),
            ProfileUpdate(kind="meetup", value="公司楼下见面", date=soon, evidence=["d2"], quote="下周三公司楼下见", confidence="medium"),
            ProfileUpdate(kind="interests", value="攀岩", evidence=["d1"], quote="编的", confidence="medium"),
        ])

    monkeypatch.setattr(profiles, "structured", fake)
    monkeypatch.setattr(person_memory, "structured", fake)
    monkeypatch.setenv("AI_ENABLED", "true")
    get_settings.cache_clear()
    assert profiles.refresh_one(crm_db, {"profile_id": str(friend)})["updates"]
    profiles.refresh_one(crm_db, {"profile_id": str(friend)})
    assert calls == ["MemoryPart", "ProfileOverview", "ProfileUpdates"]

    shown = profiles.profile_ai(friend, crm_db)
    assert shown["memory"]["summary"] == "大学同学"
    items = {item["kind"]: item for item in shown["suggestions"]}
    assert set(items) == {"birthday", "residence", "meetup", "interests"}
    assert items["residence"]["evidence"][0]["speaker"] == "TA"

    decide = lambda kind, **body: profiles.decide(friend, items[kind]["fingerprint"], profiles.Decision(**body), crm_db, user)
    decide("residence", action="accept", value="浙江杭州")
    decide("birthday", action="accept")
    decide("meetup", action="accept")
    decide("interests", action="ignore")
    assert profiles.profile_ai(friend, crm_db)["suggestions"] == []
    assert profiles.current_values(crm_db, [str(friend)])[str(friend)] == {"residence": "浙江杭州", "birthday": "03-14"}
    activity = crm_db.execute(text("""SELECT a.title, a.occurred_at::date::text FROM activities a
        JOIN activity_participants p ON p.activity_id = a.id WHERE p.profile_id = :p"""), {"p": friend}).one()
    assert tuple(activity) == ("公司楼下见面", soon)

    profiles.undo_ignore(friend, items["interests"]["fingerprint"], crm_db)
    assert [item["kind"] for item in profiles.profile_ai(friend, crm_db)["suggestions"]] == ["interests"]
    listed = profiles.list_suggestions(db=crm_db)
    assert listed["total"] == 1 and listed["kinds"] == {"interests": 1}
    get_settings.cache_clear()


def test_thin_profiles_with_recent_material_get_suggestion_only_jobs_after_the_running_pass(crm_db, monkeypatch):
    rich, speaker, poster, idle = (person(crm_db, name) for name in ("资料全", "群里常说话", "常发动态", "没动静"))
    tags(crm_db, rich, "同学", "上海", "摄影")
    for n in range(5):
        post(crm_db, rich, f"rich-{n}", f"动态 {n}")
    now = datetime.now(UTC)
    for n in range(profiles.SUGGEST_MIN_GROUP):
        group_line(crm_db, speaker, f"群{n}", "我下个月去纽约上班", now - timedelta(days=n + 1))
    for n in range(profiles.SUGGEST_MIN_POSTS):
        post(crm_db, poster, f"poster-{n}", f"搬家第 {n} 天", days_ago=300 + n)
    post(crm_db, idle, "idle-1", "很早以前", days_ago=900)
    tail = now + timedelta(hours=5)
    crm_db.execute(text("""INSERT INTO jobs(job_type, payload, available_at)
        VALUES('ai.profile', jsonb_build_object('profile_id', CAST(:id AS text)), :at)"""), {"id": str(rich), "at": tail})

    result = profiles.enqueue_all(crm_db, {})
    assert result == {"eligible": 4, "suggestion_only": 0, "queued": 3}
    queued = crm_db.execute(text("""SELECT payload, available_at FROM jobs WHERE job_type = 'ai.profile' AND available_at > :tail
                                    ORDER BY available_at"""), {"tail": tail}).all()
    assert {row.payload["profile_id"] for row in queued} == {str(speaker), str(poster), str(idle)}
    assert queued[0].available_at >= tail + timedelta(seconds=profiles.SPACING_SECONDS)

    calls = []

    def fake(model, system, payload, schema):
        calls.append((schema.__name__, len(payload.get("posts", []))))
        return ProfileUpdates(items=[])

    monkeypatch.setattr(profiles, "structured", fake)
    get_settings.cache_clear()
    monkeypatch.setattr(person_memory, "refresh", lambda *_: False)
    profiles.refresh_one(crm_db, {"profile_id": str(poster), "overview": False})
    assert calls == [("ProfileUpdates", profiles.SUGGEST_MIN_POSTS)]
    get_settings.cache_clear()
