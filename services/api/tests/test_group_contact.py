from datetime import date
from uuid import uuid4

from sqlalchemy import text

from app.contact_stats import refresh
from app.insights import recent_contacts


def profile(db, name, kind="person"):
    return db.execute(text("INSERT INTO profiles(profile_type, display_name) VALUES (:kind, :name) RETURNING id"),
                      {"kind": kind, "name": name}).scalar_one()


def account(db, wxid, owner=None):
    found = db.execute(text("""INSERT INTO source_accounts(id, provider, external_id, object_kind, display_name)
        VALUES (gen_random_uuid(), 'wechat', :wxid, 'person', :wxid) RETURNING id"""), {"wxid": wxid}).scalar_one()
    if owner:
        db.execute(text("INSERT INTO source_account_links(account_id, profile_id, evidence) VALUES (:a, :p, '{}')"),
                   {"a": found, "p": owner})
    return found


def conversation(db, kind, owner, counterpart=None):
    return db.execute(text("""INSERT INTO conversations(conversation_type, profile_id, external_id, source_account_id)
        VALUES (:kind, :owner, :external, :counterpart) RETURNING id"""),
                      {"kind": kind, "owner": owner, "external": f"{kind}:{uuid4()}", "counterpart": counterpart}).scalar_one()


def say(db, chat, at, sender_account=None, sender_profile=None):
    db.execute(text("""INSERT INTO message_events(conversation_id, occurred_at, message_type, source_record_id,
          sender_account_id, sender_profile_id) VALUES (:c, :at, '1', :r, :a, :p)"""),
               {"c": chat, "at": at, "r": str(uuid4()), "a": sender_account, "p": sender_profile})


def stats(db, person):
    return db.execute(text("""SELECT last_contact_at::date, last_direct_at::date, last_group_at::date,
          direct_message_count, group_message_count FROM profile_contact_stats WHERE profile_id = :p"""),
                      {"p": person}).one_or_none()


def test_last_contact_counts_their_group_lines_but_never_ours(crm_db):
    db = crm_db
    me, friend, other, lurker, legacy = (profile(db, name) for name in ("Me", "Friend", "Other", "Lurker", "Legacy"))
    mine, theirs, others, lurking = account(db, "wxid_me", me), account(db, "wxid_friend", friend), \
        account(db, "wxid_other", other), account(db, "wxid_lurker", lurker)
    with_friend = conversation(db, "direct", friend, theirs)
    with_other = conversation(db, "direct", other, others)
    say(db, with_friend, "2026-01-05T12:00:00Z", theirs)
    say(db, with_friend, "2026-01-06T12:00:00Z", mine)
    say(db, with_other, "2026-02-01T12:00:00Z", mine)
    room = conversation(db, "group", profile(db, "Room", "group"))
    say(db, room, "2026-03-01T12:00:00Z", theirs)
    say(db, room, "2026-03-02T12:00:00Z", theirs)
    say(db, room, "2026-03-03T12:00:00Z", lurking)
    say(db, room, "2026-03-04T12:00:00Z", mine)
    say(db, room, "2026-03-05T12:00:00Z", sender_profile=legacy)
    refresh(db)

    assert tuple(stats(db, friend)) == (date(2026, 3, 2), date(2026, 1, 6), date(2026, 3, 2), 2, 2)
    assert tuple(stats(db, other)) == (date(2026, 2, 1), date(2026, 2, 1), None, 1, 0)
    assert tuple(stats(db, lurker)) == (date(2026, 3, 3), None, date(2026, 3, 3), 0, 1)
    assert tuple(stats(db, legacy)) == (date(2026, 3, 5), None, date(2026, 3, 5), 0, 1)
    assert stats(db, me) is None

    recent = {row["name"]: row for row in recent_contacts(db, date(2026, 3, 10))}
    assert list(recent) == ["Legacy", "Lurker", "Friend", "Other"]
    assert recent["Lurker"]["group_message_count"] == 1 and "direct_message_count" not in recent["Lurker"]

    moved = profile(db, "Moved")
    db.execute(text("UPDATE source_account_links SET profile_id = :p WHERE account_id = :a"), {"p": moved, "a": lurking})
    assert stats(db, lurker) is None and stats(db, moved)[2] == date(2026, 3, 3)

    say(db, room, "2026-04-01T12:00:00Z", theirs)
    assert refresh(db) == 1
    assert stats(db, friend)[0] == date(2026, 4, 1)
