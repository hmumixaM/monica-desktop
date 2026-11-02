from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from test_media_integration import db as db  # noqa: PLC0414 -- shared pytest fixture

from app.link_ownership import link_blockers
from app.link_review import apply_batch, confirm, linked_people, review
from app.repositories import append_field_revision
from app.schemas import PlatformLinkBatch, PlatformLinkDecision


def _person(db, name):
    profile_id = uuid4()
    db.execute(
        text("INSERT INTO profiles(id,profile_type,display_name) VALUES(:id,'person',:name)"),
        {"id": profile_id, "name": name},
    )
    return profile_id


def _account(db, provider, name, username, profile_id, ready=True):
    account_id = uuid4()
    db.execute(
        text("""
          INSERT INTO source_accounts(id,provider,external_id,object_kind,display_name,username,migrated_at)
          VALUES(:id,:provider,:ext,'person',:name,:user,CASE WHEN :ready THEN now() END)
        """),
        {"id": account_id, "provider": provider, "ext": username, "name": name, "user": username, "ready": ready},
    )
    if profile_id:
        db.execute(
            text("INSERT INTO source_account_links(account_id,profile_id,evidence) VALUES(:account,:profile,'{}'::jsonb)"),
            {"account": account_id, "profile": profile_id},
        )
    return account_id


def test_review_lists_linked_people_and_confirm_moves_account(db):
    wechat_profile = _person(db, "陈艾力")
    instagram_profile = _person(db, "Alex Chen")
    other = _person(db, "完全无关")
    wechat = _account(db, "wechat", "陈艾力", "alexchen", wechat_profile)
    instagram = _account(db, "instagram", "Alex Chen", "alexchen", instagram_profile)
    _account(db, "linkedin", "Unrelated", "someone-else", other)
    result = review(db, status_filter="all")
    assert "stats" not in result
    assert linked_people(db) == []
    row = next(item for item in result["queue"] if item["account_id"] == str(instagram))
    assert row["status"] == "unresolved"
    assert row["candidates"][0]["profile_id"] == str(wechat_profile)
    assert row["candidates"][0]["score"] >= 70
    payload = PlatformLinkDecision(
        account_id=instagram,
        profile_id=wechat_profile,
        expected_profile_id=instagram_profile,
        score=row["candidates"][0]["score"],
        reasons=row["candidates"][0]["reasons"],
    )
    confirm(db, payload, {"id": None})
    db.commit()
    assert db.execute(
        text("SELECT profile_id FROM source_account_links WHERE account_id=:id"), {"id": instagram}
    ).scalar_one() == wechat_profile
    assert db.execute(
        text("SELECT profile_id FROM source_account_links WHERE account_id=:id"), {"id": wechat}
    ).scalar_one() == wechat_profile
    linked = linked_people(db)
    assert {person["profile_id"] for person in linked} == {str(wechat_profile)}
    assert set(linked[0]["providers"]) == {"instagram", "wechat"}
    assert {account["provider"] for account in linked[0]["accounts"]} == {"instagram", "wechat"}
    assert linked_people(db, providers="wechat,linkedin") == []
    assert linked_people(db, query="alexchen")[0]["profile_id"] == str(wechat_profile)
    assert linked_people(db, query="nobody") == []


def test_review_offers_one_direction_for_mutual_suggestions(db):
    wechat_profile = _person(db, "陈艾力")
    instagram_profile = _person(db, "Alex Chen")
    wechat = _account(db, "wechat", "陈艾力", "alexchen", wechat_profile)
    instagram = _account(db, "instagram", "Alex Chen", "alexchen", instagram_profile)
    queue = {row["account_id"]: row for row in review(db)["queue"]}
    assert str(wechat) not in queue
    assert queue[str(instagram)]["candidates"][0]["profile_id"] == str(wechat_profile)


def test_group_display_names_match_and_are_shown_with_paging(db):
    wechat_profile = _person(db, "小鱼")
    _account(db, "wechat", "小鱼", "wxid_fish", wechat_profile)
    append_field_revision(
        db, profile_id=wechat_profile, field_key="wechat.group_nicknames",
        value="2019 QDAA｜Richard Li Zhitong\nPhysics｜RLZ", source_type="wechat", operation="import",
    )
    instagram = _account(db, "instagram", "Richard Li Zhitong", "richard.lzt", _person(db, "Richard Li Zhitong"))
    for number in range(3):
        _account(db, "linkedin", f"Other Person {number}", f"other-{number}", _person(db, f"Other Person {number}"))
        _account(db, "wechat", f"Other Person {number}", f"wxid_other_{number}", _person(db, f"其他 {number}"))

    first = review(db, page_size=2)
    assert first["total"] > 2 and len(first["queue"]) == 2
    rest = review(db, page=2, page_size=2)["queue"]
    assert not {row["account_id"] for row in first["queue"]} & {row["account_id"] for row in rest}

    row = next(item for item in review(db, query="Richard", page_size=200)["queue"] if item["account_id"] == str(instagram))
    candidate = next(item for item in row["candidates"] if item["profile_id"] == str(wechat_profile))
    assert candidate["signals"] == [{"label": "群内名", "values": ["Richard Li Zhitong（2019 QDAA）", "RLZ（Physics）"], "more": 0}]
    assert "Richard Li Zhitong" in " ".join(candidate["reasons"])


def test_unmigrated_wechat_confirms_unless_ownership_is_ambiguous(db):
    wechat_profile = _person(db, "陈艾力")
    instagram_profile = _person(db, "Alex Chen")
    occupied = _person(db, "Already has Instagram")
    unmigrated = _account(db, "wechat", "陈艾力", "alexchen", wechat_profile, ready=False)
    instagram = _account(db, "instagram", "Alex Chen", "alexchen", instagram_profile)
    _account(db, "instagram", "Other", "otherig", occupied)
    assert link_blockers(db, [unmigrated]) == {}

    tag = db.execute(text("INSERT INTO tags(name) VALUES('Imported') RETURNING id")).scalar_one()
    db.execute(
        text("INSERT INTO tag_memberships(tag_id,profile_id,source_type) VALUES(:tag,:profile,'wechat')"),
        {"tag": tag, "profile": wechat_profile},
    )
    assert link_blockers(db) == {str(unmigrated): "unowned_tags"}
    with pytest.raises(HTTPException) as blocked:
        confirm(
            db,
            PlatformLinkDecision(account_id=unmigrated, profile_id=instagram_profile, expected_profile_id=wechat_profile),
            {"id": None},
        )
    assert blocked.value.status_code == 409

    db.execute(text("DELETE FROM tag_memberships WHERE tag_id=:tag"), {"tag": tag})
    confirm(
        db,
        PlatformLinkDecision(account_id=unmigrated, profile_id=instagram_profile, expected_profile_id=wechat_profile),
        {"id": None},
    )
    assert db.execute(
        text("SELECT profile_id FROM source_account_links WHERE account_id=:id"), {"id": unmigrated}
    ).scalar_one() == instagram_profile
    with pytest.raises(HTTPException) as duplicate:
        confirm(
            db,
            PlatformLinkDecision(account_id=instagram, profile_id=occupied, expected_profile_id=instagram_profile),
            {"id": None},
        )
    assert duplicate.value.status_code == 409
    assert db.execute(text("SELECT count(*) FROM source_account_links")).scalar_one() == 3


def test_batch_applies_each_item_independently(db):
    wechat_profile = _person(db, "陈艾力")
    instagram_profile = _person(db, "Alex Chen")
    linkedin_profile = _person(db, "Alex C.")
    occupied = _person(db, "Already has Instagram")
    unrelated = _person(db, "Someone")
    _account(db, "wechat", "陈艾力", "alexchen", wechat_profile)
    instagram = _account(db, "instagram", "Alex Chen", "alexchen", instagram_profile)
    linkedin = _account(db, "linkedin", "Alex C.", "alex-c", linkedin_profile)
    _account(db, "instagram", "Other", "otherig", occupied)
    stale = _account(db, "linkedin", "Moved", "moved", unrelated)
    skipped = _account(db, "linkedin", "Skip me", "skip-me", _person(db, "Skip me"))

    result = apply_batch(db, PlatformLinkBatch(items=[
        {"action": "confirm", "account_id": instagram, "profile_id": wechat_profile, "expected_profile_id": instagram_profile},
        {"action": "confirm", "account_id": linkedin, "profile_id": wechat_profile, "expected_profile_id": linkedin_profile},
        {"action": "confirm", "account_id": stale, "profile_id": occupied, "expected_profile_id": wechat_profile},
        {"action": "skip", "account_id": skipped, "profile_id": unrelated},
    ]), {"id": None})
    db.commit()

    outcome = {item["account_id"]: item for item in result["results"]}
    assert outcome[str(instagram)]["ok"] and outcome[str(linkedin)]["ok"] and outcome[str(skipped)]["ok"]
    assert outcome[str(stale)] == {
        "account_id": str(stale), "ok": False, "error": "The account link changed; refresh before confirming",
    }
    links = dict(db.execute(text("SELECT account_id,profile_id FROM source_account_links")).all())
    assert links[instagram] == wechat_profile and links[linkedin] == wechat_profile
    assert links[stale] == unrelated
    assert db.execute(
        text("SELECT status FROM platform_link_decisions WHERE source_account_id=:id"), {"id": skipped}
    ).scalar_one() == "skipped"
    assert {person["profile_id"] for person in linked_people(db, providers="wechat,instagram,linkedin")} == {str(wechat_profile)}
