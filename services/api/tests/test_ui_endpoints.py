import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from app.config import get_settings
from app.contact_stats import refresh


def person(db, name, **extra):
    pid = uuid4()
    db.execute(text("INSERT INTO profiles(id,profile_type,display_name) VALUES(:id,:kind,:name)"),
               {"id": pid, "kind": extra.get("kind", "person"), "name": name})
    return pid


def test_experiences_reads_current_facts_and_entity_names_with_profile_scope(crm_db):
    from app.profile_facts import experiences
    from app.project_profiles import _project_structured_fact

    first, second = person(crm_db, "First"), person(crm_db, "Second")

    def fact(pid, kind, key, value):
        _project_structured_fact(crm_db, profile_id=pid, fact_type=kind, value=value,
                                 source_type="linkedin", source_record_id=str(pid),
                                 observed_at=datetime.now(UTC), fact_key_override=key)

    fact(first, "employment", "job", {"company": "Original Co", "title": "Old role"})
    fact(first, "employment", "job", {"company": "Original Co", "title": "Engineer"})
    fact(first, "education", "degree", {"school_name": "Original School", "degree": "BS"})
    fact(first, "address", "home", {"city": "Chicago"})
    fact(second, "employment", "job", {"company_name": "Fallback Co", "title": "Writer"})
    fact(second, "education", "degree", {"school_name": "Fallback School", "degree": "MA"})
    crm_db.execute(text("UPDATE companies SET name='Renamed Co' WHERE name='Original Co'"))
    crm_db.execute(text("UPDATE schools SET name='Renamed School' WHERE name='Original School'"))
    crm_db.execute(text("UPDATE profile_employment_revisions SET company_id=NULL WHERE profile_id=:pid"), {"pid": second})
    crm_db.execute(text("UPDATE profile_education_revisions SET school_id=NULL WHERE profile_id=:pid"), {"pid": second})

    result = experiences(crm_db)
    assert set(result) == {str(first), str(second)}
    job, education = result[str(first)]
    assert (job["type"], job["company"], job["title"], job["source"]) == ("employment", "Renamed Co", "Engineer", "linkedin")
    assert job["company_id"] and education["school_id"]
    assert (education["type"], education["school"], education["degree"]) == ("education", "Renamed School", "BS")
    assert result[str(second)] == [
        {"type": "employment", "source": "linkedin", "company": "Fallback Co", "title": "Writer"},
        {"type": "education", "source": "linkedin", "school": "Fallback School", "degree": "MA"},
    ]
    assert experiences(crm_db, first) == {str(first): result[str(first)]}
    assert experiences(crm_db, uuid4()) == {}


def test_field_texts_batches_values_and_can_exclude_archived_profiles(crm_db):
    from app.profile_facts import field_texts
    from app.repositories import append_field_revision

    active, archived = person(crm_db, "Active"), person(crm_db, "Archived")
    for pid in (active, archived):
        for key, value in (("wechat.nickname", "Old"), ("wechat.nickname", " Shared "),
                           ("wechat.city", 123), ("wechat.alias", False), ("unrelated", "Ignore")):
            append_field_revision(crm_db, profile_id=pid, field_key=key, value=value,
                                  source_type="manual", operation="manual_edit")
    crm_db.execute(text("UPDATE profiles SET archived_at=now() WHERE id=:id"), {"id": archived})
    expected = {"wechat.nickname": "Shared", "wechat.city": "123"}
    assert field_texts(crm_db) == {str(active): expected, str(archived): expected}
    assert field_texts(crm_db, active_only=True) == {str(active): expected}
    assert field_texts(crm_db, profile_id=archived) == {str(archived): expected}
    assert field_texts(crm_db, keys=["wechat.nickname"], profile_id=active) == {str(active): {"wechat.nickname": "Shared"}}
    assert field_texts(crm_db, profile_id=uuid4()) == {}


def test_content_payload_batches_preserve_all_values_and_ignore_missing_ids(crm_db):
    from app.profile_facts import _content_payloads

    rows = crm_db.execute(text("""INSERT INTO content_objects(sha256,content_kind,byte_length,payload_json)
        SELECT md5(n::text)||md5(n::text),'field_value',1,jsonb_build_object('value',n)
        FROM generate_series(1,1100) n RETURNING id,payload_json""")).all()
    expected = dict(rows)
    ids = list(expected)
    assert _content_payloads(crm_db, [*ids, *ids[:20], uuid4()]) == expected
    assert _content_payloads(crm_db, []) == {}


def account(db, external):
    return db.execute(text("""INSERT INTO source_accounts(id,provider,external_id,object_kind,display_name)
        VALUES(gen_random_uuid(),'wechat',:e,'person',:e) RETURNING id"""), {"e": external}).scalar_one()


def media(db, suffix="a", width=None, height=None):
    content = db.execute(text("""INSERT INTO content_objects(sha256,content_kind,byte_length,storage_uri)
        VALUES(:sha,'media',1,'x') RETURNING id"""), {"sha": suffix.ljust(64, "0")}).scalar_one()
    return db.execute(text("""INSERT INTO media_assets(content_object_id,sha256,media_type,byte_length,object_path,width,height)
        VALUES(:c,:sha,'image/jpeg',1,'/tmp/x',:w,:h) RETURNING id"""),
        {"c": content, "sha": suffix.ljust(64, "0"), "w": width, "h": height}).scalar_one()


def chat(db, pid, external, messages):
    them, me = account(db, external), account(db, external + "-me")
    conversation = db.execute(text("""INSERT INTO conversations(conversation_type,profile_id,external_id,source_account_id)
        VALUES('direct',:p,:e,:a) RETURNING id"""), {"p": pid, "e": "direct:" + external, "a": them}).scalar_one()
    for n, (at, mine, body) in enumerate(messages):
        message = db.execute(text("""INSERT INTO message_events(conversation_id,occurred_at,message_type,source_record_id,sender_account_id)
            VALUES(:c,:at,'1',:r,:s) RETURNING id"""), {"c": conversation, "at": at, "r": f"{external}-{n}", "s": me if mine else them}).scalar_one()
        db.execute(text("INSERT INTO message_search(message_id,occurred_at,conversation_id,body) VALUES(:m,:at,:c,:b)"),
                   {"m": message, "at": at, "c": conversation, "b": body})
    return conversation


def test_overview_omits_empty_blocks_and_includes_present_ones(crm_db):
    from app.overview import profile_overview

    empty = person(crm_db, "Nobody")
    result = profile_overview(profile_id=empty, db=crm_db, _user=None)
    assert set(result) == {"header"}
    assert result["header"] == {"id": str(empty), "type": "person", "name": "Nobody"}

    friend = person(crm_db, "Friend")
    now = datetime.now(UTC)
    chat(crm_db, friend, "wxid_friend", [(now - timedelta(days=40), False, "hello"), (now, True, "see you")])
    refresh(crm_db)
    tag = crm_db.execute(text("INSERT INTO tags(name) VALUES('Close') RETURNING id")).scalar_one()
    crm_db.execute(text("INSERT INTO tag_memberships(tag_id,profile_id,source_type) VALUES(:t,:p,'manual')"), {"t": tag, "p": friend})
    image = media(crm_db, "b", 800, 600)
    body = crm_db.execute(text("""INSERT INTO content_objects(sha256,content_kind,byte_length,payload_json)
        VALUES(:sha,'post_body',1,jsonb_build_object('text',CAST(:body AS text))) RETURNING id"""),
        {"sha": "e".ljust(64, "0"), "body": "长文 " * 100}).scalar_one()
    post = crm_db.execute(text("""INSERT INTO social_posts(provider,external_id,profile_id,occurred_at,content_object_id,current_content_hash)
        VALUES('wechat','p1',:p,now(),:c,'h') RETURNING id"""), {"p": friend, "c": body}).scalar_one()
    crm_db.execute(text("INSERT INTO media_links(media_id,entity_type,entity_id,role) VALUES(:m,'social_post',:p,'media-1')"), {"m": image, "p": post})
    result = profile_overview(profile_id=friend, db=crm_db, _user=None)
    assert result["messages"]["total"] == 2 and len(result["messages"]["months"]) == 24
    assert (result["messages"]["sent"], result["messages"]["received"]) == (1, 1)
    assert result["tags"] == [{"id": str(tag), "name": "Close"}]
    recent = result["recent_posts"][0]
    assert recent["thumbnail_media_id"] == str(image) and (recent["width"], recent["height"]) == (800, 600)
    assert len(recent["text"]) <= 120
    assert "contact_events" not in result and "relationships" not in result and "groups" not in result


def test_home_counts_and_insights_list_attention(crm_db):
    from app.home import home
    from app.insights import insights

    now = datetime.now(UTC)
    recent, dormant = person(crm_db, "Recent"), person(crm_db, "Dormant")
    chat(crm_db, recent, "wxid_recent", [(now - timedelta(days=1), False, "hi")])
    chat(crm_db, dormant, "wxid_dormant", [(now - timedelta(days=200 + n), n % 2 == 0, "old") for n in range(5)])
    refresh(crm_db)
    today = datetime.now(ZoneInfo(get_settings().crm_timezone)).date()
    soon, past = today + timedelta(days=3), today - timedelta(days=2)
    crm_db.execute(text("INSERT INTO profile_dates(profile_id,kind,date_precision,month,day) VALUES(:p,'birthday','month_day',:m,:d)"),
                   {"p": recent, "m": soon.month, "d": soon.day})
    crm_db.execute(text("""INSERT INTO profile_dates(profile_id,kind,label,date_precision,year,month,day)
        VALUES(:p,'anniversary','结婚纪念日','exact',:y,:m,:d)"""), {"p": dormant, "y": past.year - 5, "m": past.month, "d": past.day})
    crm_db.execute(text("""INSERT INTO social_posts(provider,external_id,profile_id,occurred_at,current_content_hash)
        VALUES('instagram','fresh',:p,now(),'h')"""), {"p": recent})
    counts = home(db=crm_db, _user=None)
    assert counts["key_numbers"]["contacts"] == 2 and counts["key_numbers"]["messages_30d"] == 1
    assert "recent_contacts" not in counts and "top_companies" not in counts
    result = insights(dormant_days=90, db=crm_db, _user=None)
    assert result["recent_contacts"][0]["name"] == "Recent" and len(result["recent_contacts"][0]["months"]) == 12
    assert [p["name"] for p in result["dormant_contacts"]] == ["Dormant"]
    assert result["upcoming_dates"][0]["name"] == "Recent" and result["upcoming_dates"][0]["kind"] == "birthday"
    assert result["recent_dates"][0] == {"profile_id": str(dormant), "name": "Dormant", "kind": "anniversary",
                                         "label": "结婚纪念日", "date": past.isoformat(), "days_ago": 2}
    assert result["recent_posters"][0] == {"profile_id": str(recent), "name": "Recent", "posts": 1,
                                           "latest_at": result["recent_posters"][0]["latest_at"], "providers": ["instagram"]}
    assert "dormant_contacts" not in insights(dormant_days=365, db=crm_db, _user=None)


def test_directory_snapshot_encodes_search_and_sort_fields(crm_db):
    from app.directory import directory_snapshot

    pid = person(crm_db, "Alice")
    group = person(crm_db, "Group", kind="group")
    crm_db.execute(text("INSERT INTO group_memberships(group_profile_id,person_profile_id) VALUES(:g,:p)"), {"g": group, "p": pid})
    chat(crm_db, pid, "wxid_alice", [(datetime.now(UTC), False, "hi")])
    refresh(crm_db)
    crm_db.execute(text("INSERT INTO profile_dates(profile_id,kind,date_precision,month,day) VALUES(:p,'birthday','month_day',2,29)"), {"p": pid})
    crm_db.execute(text("INSERT INTO profile_dates(profile_id,kind,date_precision,month,day) VALUES(:p,'anniversary','month_day',5,1)"), {"p": pid})
    snapshot = directory_snapshot(db=crm_db, _user=None)
    columns = snapshot["columns"]
    rows = {row[columns.index("name")]: dict(zip(columns, row, strict=True)) for row in snapshot["profiles"]}
    assert rows["Alice"]["birthday_md"] == "02-29" and rows["Alice"]["direct_message_count"] == 1
    assert rows["Alice"]["last_contact_at"] and rows["Alice"]["conversation_id"]
    assert rows["Group"]["member_count"] == 1 and rows["Alice"]["member_count"] is None


def test_group_display_names_are_searchable_without_repeating_known_names():
    from app.profile_facts import group_names

    value = "2019 QDAA｜Richard 李治彤\nBob捏mimimi｜十七只孑孓\n另一个群 | alice\n没有分隔的名字\n空｜"
    assert group_names(value, ["Alice", "Richard 李治彤"]) == ["十七只孑孓", "没有分隔的名字"]
    assert group_names(None, []) == []


def test_timeline_marks_ready_media_filters_and_nulls_placeholders(crm_db):
    from app.routers.timeline import timeline

    pid = person(crm_db, "Poster")
    local = media(crm_db, "c")
    for external, summary in (("with-media", "Real caption"), ("remote-only", "partial")):
        content = crm_db.execute(text("""INSERT INTO content_objects(sha256,content_kind,byte_length,payload_json)
            VALUES(:sha,'post_body',1,CAST(:payload AS jsonb)) RETURNING id"""),
            {"sha": external.ljust(64, "0"), "payload": json.dumps({"text": summary, "media": [{"source_url": f"https://x/{external}"}]})}).scalar_one()
        post = crm_db.execute(text("""INSERT INTO social_posts(provider,external_id,profile_id,occurred_at,content_object_id,current_content_hash)
            VALUES('instagram',:e,:p,now(),:c,'h') RETURNING id"""), {"e": external, "p": pid, "c": content}).scalar_one()
        if external == "with-media":
            crm_db.execute(text("INSERT INTO media_links(media_id,entity_type,entity_id,role) VALUES(:m,'social_post',:p,'media-1')"), {"m": local, "p": post})
    everything = {row["external_id"]: row for row in timeline(profile_id=pid, db=crm_db, _user=None)}
    assert everything["remote-only"]["summary"] is None
    assert everything["remote-only"]["media"][0]["ready"] is False
    assert any(item["ready"] for item in everything["with-media"]["media"])
    ready = timeline(profile_id=pid, media="ready", db=crm_db, _user=None)
    assert [row["external_id"] for row in ready] == ["with-media"]
    assert all(item["ready"] for item in ready[0]["media"])


def test_relationship_rows_and_conversations_carry_avatars_and_previews(crm_db):
    from app.main import profile_relationships
    from app.routers.messages import list_conversations

    owner, friend = person(crm_db, "Owner"), person(crm_db, "Friend")
    avatar = media(crm_db, "d")
    crm_db.execute(text("UPDATE profiles SET avatar_media_id=:m WHERE id=:p"), {"m": avatar, "p": friend})
    kind = crm_db.execute(text("INSERT INTO relationship_types(name) VALUES('friend') RETURNING id")).scalar_one()
    crm_db.execute(text("""INSERT INTO relationship_edges(from_profile_id,to_profile_id,relationship_type_id,source_type)
        VALUES(:a,:b,:t,'manual')"""), {"a": owner, "b": friend, "t": kind})
    rows = profile_relationships(profile_id=owner, db=crm_db, _user=None)
    assert rows[0]["avatar_media_id"] == str(avatar)
    chat(crm_db, friend, "wxid_prev", [(datetime.now(UTC), False, "  last\n message ")])
    refresh(crm_db)
    conversation = list_conversations(db=crm_db, _user=None)[0]
    assert conversation["last_message_preview"] == "last message"
    assert str(conversation["avatar_media_id"]) == str(avatar) and conversation["cursor"]


def test_images_are_served_as_originals_with_recorded_dimensions(crm_db, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from PIL import Image

    from app import main
    from app.archive_media import register_file
    from app.media_dimensions import backfill, record_dimensions
    from app.routers import media as media_routes

    photo = tmp_path / "rotated.jpg"
    exif = Image.Exif()
    exif[0x0112] = 6
    Image.new("RGB", (1000, 500), "blue").save(photo, exif=exif)
    media_id = register_file(crm_db, photo, tmp_path / "media", "wechat")
    queued = crm_db.execute(text("SELECT payload->>'media_id' FROM jobs WHERE job_type='media.dimensions'")).scalars().all()
    assert queued == [str(media_id)]
    assert record_dimensions(crm_db, media_id)
    assert crm_db.execute(text("SELECT width,height FROM media_assets WHERE id=:id"), {"id": media_id}).one() == (500, 1000)
    assert backfill(crm_db)["checked"] == 0
    monkeypatch.setattr(main.settings, "media_root", tmp_path / "media")
    crm_db.commit()
    response = media_routes.download_media(media_id, SimpleNamespace(headers={}), db=crm_db, _user={})
    assert response.media_type == "image/jpeg" and "immutable" in response.headers["cache-control"]
    cached = media_routes.download_media(media_id, SimpleNamespace(headers={"if-none-match": response.headers["etag"]}),
                                 db=crm_db, _user={})
    assert cached.status_code == 304


def test_recording_image_dimensions_keeps_the_media_version(crm_db):
    from app.data_versions import current

    image = media(crm_db, "f")
    before = current(crm_db, ("media",))["media"]
    crm_db.execute(text("UPDATE media_assets SET width=10, height=20 WHERE id=:id"), {"id": image})
    assert current(crm_db, ("media",))["media"] == before
    crm_db.execute(text("UPDATE media_assets SET object_path='/tmp/moved' WHERE id=:id"), {"id": image})
    assert current(crm_db, ("media",))["media"] == before + 1


def test_experience_is_deduplicated_and_ordered_current_then_newest():
    from app.profile_facts import ordered_experience

    facts = [
        {"type": "education", "school": "UNC", "degree": "BS", "start_date": "2019-08", "end_date": "2023-05"},
        {"type": "employment", "company": "Old Co", "title": "Intern", "start_date": "2022-06", "end_date": "2022-08"},
        {"type": "employment", "company": "Epic", "title": "Engineer", "start_date": "2024-10", "is_current": True},
        {"type": "employment", "company": "Old Co", "title": "Intern", "start_date": "2022-06", "end_date": "2022-08"},
        {"type": "employment", "company": "Mid Co", "title": "Analyst", "start_date": "2023-06", "end_date": "2024-09"},
    ]
    assert [f.get("company") or f.get("school") for f in ordered_experience(facts)] == ["Epic", "Mid Co", "Old Co", "UNC"]


def test_significant_dates_api_keeps_one_birthday_and_orders_it_first(crm_db):
    from fastapi import HTTPException

    from app.main import profile as profile_detail
    from app.routers.profile_dates import (
        add_significant_date,
        edit_significant_date,
        remove_significant_date,
    )
    from app.schemas import SignificantDateWrite

    user = {"id": None}
    pid = person(crm_db, "Dated")
    anniversary = add_significant_date(pid, SignificantDateWrite(kind="anniversary", label="认识的日子", precision="exact",
                                                                  year=2015, month=4, day=2), db=crm_db, user=user)
    birthday = add_significant_date(pid, SignificantDateWrite(kind="birthday", precision="month_day", month=12, day=1),
                                    db=crm_db, user=user)
    again = add_significant_date(pid, SignificantDateWrite(kind="anniversary", label="认识的日子", precision="exact",
                                                            year=2015, month=4, day=2), db=crm_db, user=user)
    assert again["id"] == anniversary["id"]
    with pytest.raises(HTTPException) as duplicate:
        add_significant_date(pid, SignificantDateWrite(kind="birthday", precision="month_day", month=1, day=1), db=crm_db, user=user)
    assert duplicate.value.status_code == 409 and duplicate.value.detail == "birthday_exists"
    profile = profile_detail(pid, db=crm_db, _user=user)
    assert [d["id"] for d in profile["significant_dates"]] == [birthday["id"], anniversary["id"]]
    assert profile["birthday"]["id"] == birthday["id"]
    moved = edit_significant_date(pid, anniversary["id"], SignificantDateWrite(kind="other", label="搬家", precision="approximate",
                                                                              year=2030), db=crm_db, user=user)
    assert moved["kind"] == "other" and moved["year"] == 2030
    remove_significant_date(pid, birthday["id"], db=crm_db, user=user)
    assert profile_detail(pid, db=crm_db, _user=user)["birthday"] is None


def test_a_removed_imported_tag_stays_removed_until_added_back(crm_db):
    from fastapi import HTTPException

    from app.full_migration import add_tag as import_tag
    from app.main import attach_tag, detach_tag

    user = {"id": None}
    pid = person(crm_db, "Tagged")
    import_tag(crm_db, name="同学", profile_id=str(pid), source_type="wechat", source_record_id="label:1")
    tag = crm_db.execute(text("SELECT id FROM tags WHERE name = '同学'")).scalar_one()
    detach_tag(pid, tag, db=crm_db, user=user)
    import_tag(crm_db, name="同学", profile_id=str(pid), source_type="wechat", source_record_id="label:1")
    assert crm_db.execute(text("SELECT count(*) FROM tag_memberships WHERE profile_id = :p"), {"p": pid}).scalar_one() == 0
    with pytest.raises(HTTPException):
        detach_tag(pid, tag, db=crm_db, user=user)
    attach_tag(pid, tag, db=crm_db, user=user)
    assert crm_db.execute(text("SELECT source_type FROM tag_memberships WHERE profile_id = :p"), {"p": pid}).scalar_one() == "manual"
    assert crm_db.execute(text("SELECT count(*) FROM tag_membership_removals")).scalar_one() == 0
