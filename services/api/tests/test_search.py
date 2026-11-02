from uuid import uuid4

from sqlalchemy import text

from app.search import escape_like, search


def person(db, name, summary=None):
    pid = uuid4()
    db.execute(text("INSERT INTO profiles(id,profile_type,display_name,summary) VALUES(:id,'person',:name,:summary)"),
               {"id": pid, "name": name, "summary": summary})
    return pid


def test_like_wildcards_are_literal():
    assert escape_like(r"50%_off\x") == r"50\%\_off\\x"


def test_each_kind_is_limited_separately_and_posts_name_their_author(crm_db):
    owner = person(crm_db, "Owner")
    for n in range(4):
        # search_body is projected from the post's content object by a trigger.
        content = crm_db.execute(text("""INSERT INTO content_objects(sha256,content_kind,payload_json)
            VALUES(:sha,'post','{"caption":"hiking trip"}'::jsonb) RETURNING id"""), {"sha": f"{n}".ljust(64, "0")}).scalar_one()
        crm_db.execute(text("""INSERT INTO social_posts(provider,external_id,profile_id,occurred_at,current_content_hash,content_object_id)
            VALUES('instagram',:e,:p,now() - make_interval(days => :n),'h',:c)"""), {"e": f"p{n}", "p": owner, "n": n, "c": content})
    hiker = person(crm_db, "Hiking Club")
    person(crm_db, "Someone", summary="likes hiking")
    crm_db.execute(text("INSERT INTO tags(name) VALUES('hiking')"))
    activity = crm_db.execute(text("""INSERT INTO activities(activity_type,occurred_at,title) VALUES('note',now(),'hiking plan')
        RETURNING id""")).scalar_one()
    crm_db.execute(text("INSERT INTO activity_participants(activity_id,profile_id) VALUES(:a,:p)"), {"a": activity, "p": owner})

    rows = search(crm_db, "hiking", limit=2)
    kinds = [row["entity_type"] for row in rows]
    assert kinds.count("profile") == 2 and kinds.count("timeline") == 2 and kinds.count("tag") == 1 and kinds.count("activity") == 1
    assert next(row for row in rows if row["entity_type"] == "profile")["id"] == hiker
    post = next(row for row in rows if row["entity_type"] == "timeline")
    assert post["title"] == "Owner" and post["profile_id"] == owner and post["kind"] == "post" and post["provider"] == "instagram"
    note = next(row for row in rows if row["entity_type"] == "activity")
    assert note["profile_name"] == "Owner"


def test_short_and_wildcard_queries_match_nothing_extra(crm_db):
    person(crm_db, "Alice")
    assert search(crm_db, "a") == []
    assert search(crm_db, "%%") == []
