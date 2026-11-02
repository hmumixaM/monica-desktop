import json
from io import BytesIO

from app.chatlog_proxy import filter_messages, group_speech, sender_keys, spoken_by


def test_only_the_requested_person_is_kept():
    messages = [
        {"sender": "别人", "content": "别人:\n不是他", "time": "2026-01-01 00:00", "timestamp": 1, "type": "text"},
        {"sender": "zzz", "content": "FixtureSender:\n测试旅行建议", "time": "2026-04-22 20:13", "timestamp": 2, "type": "text"},
        {"sender": "zzz", "content": "FixtureSender:\n<?xml version=\"1.0\"?><title>这条测试多少钱</title>", "time": "2026-07-22 05:09", "timestamp": 3, "type": "quote"},
    ]
    kept = filter_messages(messages, {"fixturesender"}, "多少", 10)
    assert [row["text"] for row in kept] == ["这条测试多少钱"]


def test_a_shared_display_name_does_not_count_as_the_sender():
    keys = sender_keys([{"external_id": "wxid_a", "username": "wxid_a", "display_name": "fixture_sender"}])
    assert "fixture_sender" not in keys
    assert spoken_by({"sender": "fixture_sender", "content": "fixture_sender:\n求拉"}, keys) is False
    assert spoken_by({"sender": "群昵称", "content": "wxid_a:\n求拉"}, keys) is True


def test_a_different_wechat_with_the_same_nickname_is_excluded():
    messages = [
        {"sender": "zzz", "content": "other-wxid:\n同一昵称", "time": "t", "timestamp": 1, "type": "text"},
    ]
    assert spoken_by(messages[0], {"fixturesender"}) is False
    assert filter_messages(messages, {"fixturesender"}, "", 10) == []


def test_group_speech_filters_after_chatlog_returns_and_does_not_store_rows():
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b""

    def opener(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        payload = json.dumps(
            {"messages": [{"sender": "zzz", "content": "FixtureSender:\n测试酒店预订", "time": "2026-06-08", "timestamp": 9, "type": "text"}]}
        ).encode()

        class Body(BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        return Body(payload)

    result = group_speech(
        senders=[{"external_id": "FixtureSender", "username": "FixtureSender", "display_name": "zzz"}],
        groups=[{"group_id": "room", "name": "测试旅行群", "chat": "10000000001@chatroom"}],
        query="酒店",
        since="2026-01-01",
        until="2026-12-31",
        limit=10,
        base_url="http://chatlog.local",
        timeout=12,
        opener=opener,
    )
    assert "chat=10000000001%40chatroom" in captured["url"]
    assert captured["timeout"] == 12
    assert result["groups"][0]["messages"][0]["text"] == "测试酒店预订"


def test_imported_speech_shows_when_the_recent_chatlog_window_misses_it():
    def opener(request, timeout):
        payload = json.dumps(
            {"messages": [{"sender": "别人", "content": "别人:\n最近的消息", "time": "2026-09-29 01:00", "type": "text"}]}
        ).encode()

        class Body(BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        return Body(payload)

    result = group_speech(
        senders=[{"external_id": "wxid_fixture_sender", "username": "wxid_fixture_sender", "display_name": "fixture_sender"}],
        groups=[{"group_id": "fixture-room-2", "name": "测试班级群", "chat": "10000000002@chatroom"}],
        query="",
        since="",
        until="",
        limit=10,
        base_url="http://chatlog.local",
        timeout=5,
        opener=opener,
        imported=[{
            "group_id": "fixture-room-2",
            "name": "测试班级群",
            "chat": "10000000002@chatroom",
            "time": "2020-09-16 12:14",
            "content": "wxid_fixture_sender:\n这是一条测试群消息",
        }],
    )
    assert [row["text"] for row in result["groups"][0]["messages"]] == ["这是一条测试群消息"]


def test_live_speech_has_shared_deadline_and_two_request_capacity(monkeypatch):
    import asyncio

    from app import chatlog_proxy
    active = peak = 0
    class Client:
        def __init__(self, **_): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False
        async def get(self, *_args, **_kwargs):
            nonlocal active, peak
            active += 1; peak = max(peak, active)
            try:
                await asyncio.sleep(1)
            finally:
                active -= 1
    monkeypatch.setattr(chatlog_proxy.httpx, 'AsyncClient', Client)
    groups = [{'chat': f'{i}@chatroom'} for i in range(10)]
    result = asyncio.run(chatlog_proxy._live_speech([], groups, '', '', '', 50, 'http://example', [], budget=.02))
    assert peak == 2 and active == 0
    assert result['partial'] and len(result['groups']) == 10
    assert all(room['next_window'] == 0 and room['partial'] for room in result['groups'])


def test_stored_speech_is_bounded_to_selected_rooms(crm_db):
    from sqlalchemy import text

    from app.chatlog_proxy import imported_group_messages, spoken_rooms
    person = crm_db.execute(text("INSERT INTO profiles(profile_type,display_name) VALUES('person','Speaker') RETURNING id")).scalar_one()
    groups = [crm_db.execute(text("INSERT INTO profiles(profile_type,display_name) VALUES('group',:name) RETURNING id"), {'name': name}).scalar_one() for name in ('First','Second')]
    content = crm_db.execute(text("INSERT INTO content_objects(content_kind,sha256,byte_length,payload_json) VALUES('message',:sha,1,'{\"text\":\"stored\"}') RETURNING id"), {'sha': 'a'*64}).scalar_one()
    for index, group in enumerate(groups):
        room = crm_db.execute(text("INSERT INTO conversations(conversation_type,profile_id,external_id) VALUES('group',:group,:external) RETURNING id"),
                              {'group': group, 'external': f'group:{index}@chatroom'}).scalar_one()
        crm_db.execute(text("""INSERT INTO message_events(conversation_id,sender_profile_id,occurred_at,message_type,content_object_id,source_record_id)
            SELECT :room,:person,now()+make_interval(secs=>n),'text',:content,n::text FROM generate_series(1,220) n"""),
                       {'room': room, 'person': person, 'content': content})
    assert len(spoken_rooms(crm_db, person)) == 2
    messages = imported_group_messages(crm_db, person, [groups[0]])
    assert len(messages) == 200
    assert {row['group_id'] for row in messages} == {str(groups[0])}
