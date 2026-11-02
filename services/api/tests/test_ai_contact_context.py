import json
import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import event, text
from test_ai import SECRET, post
from test_ui_endpoints import person

from app.ai import communities, digest, payloads, person_memory, profiles, reconnect
from app.ai.network_context import network_context
from app.ai.prompts import VOICE_RULES
from app.ai.schemas import ProfileUpdate, ProfileUpdates
from app.profile_facts import contact_contexts, custom_fields
from app.repositories import append_field_revision


def field(db, pid, key, value, source="monica"):
    append_field_revision(db, profile_id=pid, field_key=key, value=value, source_type=source,
                          operation="manual_edit" if source == "manual" else "import")


def test_list_notes_and_group_names_reach_all_person_contexts(crm_db):
    pid = person(crm_db, "阿桥")
    field(crm_db, pid, "monica.custom_fields", {
        "教育经历": ["Brown University | 2019–2023", "UCL | 2023–2024"], "短备注": ["QD"],
        "bad": [None, {"ignore": "object"}, True], "scalar": "值"})
    field(crm_db, pid, "wechat.group_nicknames", "2019 QDAA｜Jemma徐之翘 F", "wechat")
    field(crm_db, pid, "wechat.nickname", "阿桥", "wechat")
    field(crm_db, pid, "wechat.remark", "Jemma", "wechat")
    field(crm_db, pid, "小备注", "读教育学的同学", "manual")
    field(crm_db, pid, "wechat.unapproved_field", SECRET, "wechat")
    crm_db.execute(text("UPDATE profiles SET summary='我在 QD 认识的同学' WHERE id=:id"), {"id": pid})
    note = crm_db.execute(text("""INSERT INTO activities(activity_type,occurred_at,title,body,source_type)
        VALUES('note',now(),'短笔记','她叫徐之翘','manual') RETURNING id""")).scalar_one()
    crm_db.execute(text("INSERT INTO activity_participants(activity_id,profile_id) VALUES(:a,:p)"), {"a": note, "p": pid})
    saved = custom_fields(crm_db, pid)
    assert saved["教育经历"] == "Brown University | 2019–2023\nUCL | 2023–2024"
    assert "bad" not in saved
    ctx = contact_contexts(crm_db, [str(pid)])[str(pid)]
    assert ctx["group_names"] == [{"group": "2019 QDAA", "name": "Jemma徐之翘 F"}]
    assert {row["value"] for row in ctx["names"]} == {"阿桥", "Jemma"}
    assert any(row["label"] == "小备注" and row["value"] == "读教育学的同学" for row in ctx["details"])
    assert next(d for d in ctx["details"] if d["label"] == "教育经历")["value"].splitlines() == ["Brown University | 2019–2023", "UCL | 2023–2024"]
    assert ctx["notes"][0]["body"] == "她叫徐之翘"
    assert SECRET not in json.dumps(ctx, ensure_ascii=False)
    assert SECRET not in json.dumps(person_memory._items(crm_db, str(pid)), ensure_ascii=False, default=str)
    assert payloads.overview_payload(crm_db, str(pid))["profile_context"] == ctx
    assert payloads.reconnect_payload(crm_db, str(pid), datetime.now(UTC).date(), datetime.now(UTC)-timedelta(days=90))["profile_context"] == ctx
    model_input, refs = profiles.updates_payload(crm_db, str(pid), None, datetime.now(UTC).date())
    education = next(d for d in model_input["profile_context"]["details"] if d["label"] == "教育经历")
    assert refs[education["evidence_id"]]["kind"] == "profile"
    assert "UCL" in refs[education["evidence_id"]]["text"]
    proposed = ProfileUpdates(items=[ProfileUpdate(kind="school", value="UCL", evidence=[education["evidence_id"]], quote="UCL | 2023–2024", confidence="high")])
    resolved = profiles.resolve(proposed, refs, {}, datetime.now(UTC).date()).items
    assert resolved[0]["evidence"][0]["id"] == str(pid)
    assert resolved[0]["evidence"][0]["kind"] == "profile"
    compact = contact_contexts(crm_db, [str(pid)], compact=True)
    built = payloads.digest_payload({}, [], compact)
    assert built["people_context"][str(pid)]["group_names"][0]["name"] == "Jemma徐之翘 F"
    assert len(compact[str(pid)]["details"]) <= 4
    assert any(d["label"] == "教育经历" for d in compact[str(pid)]["details"])
    assert any(d["label"] == "小备注" for d in compact[str(pid)]["details"])


def interaction(db, author, post_id, kind="like", days=1, **metadata):
    db.execute(text("""INSERT INTO social_interactions(provider,post_id,interaction_type,external_id,
        author_profile_id,occurred_at,current_content_hash,metadata)
        VALUES('wechat',:post,:kind,:ext,:author,now()-make_interval(days=>:days),:hash,CAST(:metadata AS jsonb))"""),
               {"post": post_id, "kind": kind, "ext": str(uuid4()), "author": author, "days": days,
                "hash": "h"*64, "metadata": json.dumps(metadata)})


def test_network_is_focused_directional_deduplicated_and_has_no_bodies(crm_db):
    focus, peer, other, stranger = [person(crm_db, name) for name in ("本人", "朋友", "同名", "同名")]
    for pid, external in ((focus, "focus"), (peer, "peer"), (other, "other")):
        crm_db.execute(text("INSERT INTO identities(profile_id,provider,external_id) VALUES(:p,'wechat',:e)"), {"p": pid, "e": external})
    own, theirs, third = [post(crm_db, pid, key, "文字") for pid, key in ((focus, "network-own"), (peer, "network-peer"), (other, "network-third"))]
    interaction(crm_db, focus, theirs)
    interaction(crm_db, focus, theirs)
    interaction(crm_db, peer, own, "comment")
    interaction(crm_db, peer, own, "reply", ref_author_external_id="focus")
    interaction(crm_db, peer, third, "reply", ref_author_external_id="focus")
    interaction(crm_db, peer, third, "mention", target_external_id="focus")
    interaction(crm_db, focus, theirs, "mention", target_external_id="other")
    interaction(crm_db, peer, own, "comment", days=400)
    interaction(crm_db, peer, own, "comment", is_deleted=True)
    interaction(crm_db, focus, own, "comment")
    interaction(crm_db, stranger, third)
    crm_db.execute(text("""INSERT INTO relationship_edges(from_profile_id,to_profile_id,relationship_type_id,source_type)
        SELECT :a,:b,id,'manual' FROM relationship_types WHERE name='同事'"""), {"a": focus, "b": other})
    statements = []
    listener = lambda _conn, _cursor, statement, *_args: statements.append(statement)
    conn = crm_db.connection()
    event.listen(conn, "before_cursor_execute", listener)
    network = network_context(crm_db, str(focus))
    event.remove(conn, "before_cursor_execute", listener)
    peers = network["top_peers"]
    assert {p["profile_id"] for p in peers} == {str(peer), str(other)}
    assert peers[0] | {"last_interaction": None} == {
        "profile_id": str(peer), "name": "朋友", "interactions": 6, "outgoing": 2, "incoming": 4,
        "likes": 2, "comments_and_replies": 3, "mentions": 1, "last_interaction": None}
    assert peers[1]["outgoing"] == 1 and peers[1]["mentions"] == 1
    assert network["recorded_relationships"] == [{"profile_id": str(other), "name": "同名", "relationship": "同事", "direction": "outgoing"}]
    assert not any(re.search(r"\b(body|content_objects|message_events|message_search|conversations)\b", sql) for sql in statements)
    assert payloads.overview_payload(crm_db, str(focus))["network"]["top_peers"] == peers
    assert profiles.updates_payload(crm_db, str(focus), None, datetime.now(UTC).date())[0]["network"]["top_peers"] == peers


def test_all_contact_and_batch_writing_prompts_use_my_perspective():
    for system in (person_memory.FINAL_SYSTEM, profiles.UPDATES_SYSTEM, digest.SYSTEM, reconnect.SYSTEM, communities.SYSTEM):
        assert VOICE_RULES in system
