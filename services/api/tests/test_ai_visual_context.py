from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from test_ai import post
from test_post_media import asset, photo
from test_ui_endpoints import person


def story(db, friend, external="story", days_ago=1):
    return str(db.execute(text("""INSERT INTO social_stories(provider, external_id, profile_id, occurred_at, current_content_hash)
        VALUES ('instagram', :e, :p, now() - make_interval(days => :days), :h) RETURNING id"""),
        {"e": external, "p": friend, "days": days_ago, "h": "s" * 64}).scalar_one())


def test_visual_context_reaches_all_downstream_payloads(crm_db, tmp_path):
    import json
    from uuid import uuid4

    from app.ai import payloads, person_memory, profiles, reconnect

    friend = person(crm_db, "发布者")
    confirmed = person(crm_db, "确认的人")
    possible = person(crm_db, "候选的人")
    pid = post(crm_db, friend, "image-only-context", "")
    sid = story(crm_db, friend, "context-story")
    for event_id, kind, table, key in ((pid, "social_post", "social_post_media_readings", "post_id"),
                                       (sid, "social_story", "social_story_media_readings", "story_id")):
        mid = asset(crm_db, photo(tmp_path, event_id, "green"), "image/jpeg", event_id)
        crm_db.execute(text("INSERT INTO media_links(media_id,entity_type,entity_id,role) VALUES(:m,:t,:e,'media-1')"),
                       {"m": mid, "t": kind, "e": event_id})
        crm_db.execute(text(f"""INSERT INTO {table}({key},media_ids,input_hash,model,summary,items,search_text)
            VALUES(:id,CAST(:media AS uuid[]),:hash,'test','咖啡店菜单',CAST(:items AS jsonb),'菜单')"""),
                       {"id": event_id, "media": [mid], "hash": "h" * 64,
                        "items": json.dumps([{"index": n, "description": "桌上的菜单", "text": "拿铁 32"} for n in range(1, 9)])})
        for status, profile_id, suggestion in (("confirmed", confirmed, None), ("pending", None, possible), ("ignored", None, possible)):
            crm_db.execute(text("""INSERT INTO faces(id,media_id,box,quality,status,profile_id,suggested_profile_id)
                VALUES(:id,:m,ARRAY[0.1,0.1,0.2,0.2],0.9,:s,:p,:q)"""),
                           {"id": uuid4(), "m": mid, "s": status, "p": profile_id, "q": suggestion})
    post(crm_db, friend, "placeholder-only-context", "partial")
    since = datetime.now(UTC) - timedelta(days=10)
    posts = payloads.captions(crm_db, [str(friend)], since, 20)
    assert {p["post_id"] for p in posts} == {pid, sid}
    context = posts[0]["media_context"]
    assert context["summary"] == "咖啡店菜单" and context["images"][0]["text"] == "拿铁 32"
    assert len(context["images"]) == 6
    assert context["detected_faces"] == 2
    assert context["confirmed_people"] == [{"profile_id": str(confirmed), "name": "确认的人"}]
    assert context["possible_people"] == [{"profile_id": str(possible), "name": "候选的人"}]
    assert next(p for p in posts if p["post_id"] == sid)["event_type"] == "story"
    assert payloads.overview_payload(crm_db, str(friend))["posts"] == posts
    assert payloads.reconnect_payload(crm_db, str(friend), datetime.now(UTC).date(), since)["recent_posts"] == posts
    week = payloads.week_captions(crm_db, since, datetime.now(UTC), 80)
    assert {p["post_id"] for p in week} == {pid, sid}
    built, refs = profiles.updates_payload(crm_db, str(friend), None, datetime.now(UTC).date())
    assert built["posts"][0]["media_context"] == context
    assert "拿铁 32" in refs["p1"]["text"]
    assert reconnect.cited_posts(crm_db, [sid]) == {sid: str(friend)}
    assert str(friend) in payloads.recent_posters(crm_db, since, 2)

    from app.ai.client import input_hash

    before = input_hash("test", person_memory.FINAL_SYSTEM, {"items": person_memory._items(crm_db, str(friend))})
    crm_db.execute(text("""UPDATE faces SET status='confirmed', profile_id=suggested_profile_id
        WHERE status='pending'"""))
    after = payloads.overview_payload(crm_db, str(friend))
    assert input_hash("test", person_memory.FINAL_SYSTEM, {"items": person_memory._items(crm_db, str(friend))}) != before
    assert after["posts"][0]["media_context"]["possible_people"] == []
    assert {p["name"] for p in after["posts"][0]["media_context"]["confirmed_people"]} == {"确认的人", "候选的人"}
