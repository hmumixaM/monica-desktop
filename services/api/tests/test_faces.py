import asyncio
import io
import json
import tarfile
import time
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from sqlalchemy import text
from test_ui_endpoints import account, media, person

from app.faces import review
from app.faces.device import FacesBatch
from app.faces.device import batch as store_batch
from app.faces.review import Decision, FaceState
from app.faces.signing import signed_message
from app.security import create_import_signature_for_source, hash_service_token


def link(db, media_id, entity_type, entity_id, role):
    db.execute(text("INSERT INTO media_links(media_id,entity_type,entity_id,role) VALUES(:m,:t,:e,:r)"),
               {"m": media_id, "t": entity_type, "e": entity_id, "r": role})


def post(db, author, external):
    return db.execute(text("""INSERT INTO social_posts(provider,external_id,profile_id,occurred_at,current_content_hash)
        VALUES('wechat',:e,:p,'2026-05-01',:h) RETURNING id"""), {"e": external, "p": author, "h": "h" * 64}).scalar_one()


def library(db, tmp_path):
    friend, other = person(db, "Friend"), person(db, "Other")
    avatar, photo, chat_image = media(db, "av"), media(db, "ph"), media(db, "ch")
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"jpeg")
    db.execute(text("UPDATE media_assets SET object_path = :p"), {"p": str(image)})
    unlinked = account(db, "wxid_face")
    link(db, avatar, "source_account", unlinked, "wechat_avatar_big")
    db.execute(text("INSERT INTO source_account_links(account_id,profile_id,evidence) VALUES(:a,:p,'{}'::jsonb)"),
               {"a": unlinked, "p": friend})
    moment = post(db, other, "face-post")
    link(db, photo, "social_post", moment, "media-1")
    link(db, chat_image, "message_event", uuid4(), "chat_image")
    return friend, other, avatar, photo, chat_image, moment


def signed(path, body=b"", method="GET"):
    stamp, nonce = str(int(time.time())), uuid4().hex
    parts = urlsplit(path)
    target = parts.path + (f"?{parts.query}" if parts.query else "")
    signature = create_import_signature_for_source(stamp, nonce, signed_message(method, target, body), "faces")
    return {"X-Import-Timestamp": stamp, "X-Import-Nonce": nonce, "X-Import-Signature": signature}


def test_device_sees_only_post_photos_and_avatars_and_needs_a_faces_token(crm_db, tmp_path):
    from app import main
    friend, other, avatar, photo, chat_image, _ = library(crm_db, tmp_path)
    crm_db.execute(text("INSERT INTO import_service_tokens(source,label,token_hash) VALUES('faces','home',:h)"),
                   {"h": hash_service_token("faces-token")})
    crm_db.execute(text("INSERT INTO import_service_tokens(source,label,token_hash) VALUES('wechat','w',:h)"),
                   {"h": hash_service_token("wechat-token")})
    main.app.dependency_overrides[main.get_db] = lambda: crm_db

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            headers = {**signed("/api/v1/faces/device/work?limit=10"), "X-Import-Token": "faces-token"}
            work = await client.get("/api/v1/faces/device/work?limit=10", headers=headers)
            assert work.status_code == 200
            items = work.json()["items"]
            assert [i["media_id"] for i in items] == [str(avatar), str(photo)]
            assert items[0]["owner_profile_id"] == str(friend) and items[0]["avatar"]
            assert items[1]["owner_profile_id"] == str(other) and not items[1]["avatar"]
            assert (await client.get("/api/v1/faces/device/work?limit=10", headers=headers)).status_code == 409

            other_source = {**signed("/api/v1/faces/device/work"), "X-Import-Token": "wechat-token"}
            assert (await client.get("/api/v1/faces/device/work", headers=other_source)).status_code == 401
            tampered = {**signed("/api/v1/faces/device/work?limit=1"), "X-Import-Token": "faces-token"}
            assert (await client.get("/api/v1/faces/device/work?limit=500", headers=tampered)).status_code == 401

            body = json.dumps({"ids": [str(photo), str(chat_image), str(uuid4())]}).encode()
            path = "/api/v1/faces/device/media"
            response = await client.post(path, content=body, headers={
                **signed(path, body, "POST"), "X-Import-Token": "faces-token", "Content-Type": "application/json"})
            with tarfile.open(fileobj=io.BytesIO(response.content)) as archive:
                assert {m.name: archive.extractfile(m).read() for m in archive} == {str(photo): b"jpeg"}

            body = json.dumps({"model": "buffalo_l", "scans": [
                {"media_id": str(avatar), "faces": [{"id": str(uuid4()), "box": [.2, .2, .5, .5], "quality": .9}]},
                {"media_id": str(photo), "faces": [{"id": str(uuid4()), "box": [.1, .1, .2, .2], "quality": .8}]}]
            }).encode()
            path = "/api/v1/faces/device/batch"
            stored = await client.post(path, content=body, headers={
                **signed(path, body, "POST"), "X-Import-Token": "faces-token", "Content-Type": "application/json"})
            assert stored.json() == {"faces": 2, "scans": 2, "suggestions": 0}
            headers = {**signed("/api/v1/faces/device/work"), "X-Import-Token": "faces-token"}
            assert (await client.get("/api/v1/faces/device/work", headers=headers)).json()["items"] == []
            headers = {**signed("/api/v1/faces/device/anchors"), "X-Import-Token": "faces-token"}
            anchors = (await client.get("/api/v1/faces/device/anchors", headers=headers)).json()
            assert [(a["profile_id"], a["kind"]) for a in anchors["anchors"]] == [(str(friend), "avatar")]

    try:
        asyncio.run(exercise())
    finally:
        main.app.dependency_overrides.pop(main.get_db, None)


def test_review_confirm_undo_and_not_this_person(crm_db, tmp_path):
    friend, other, _avatar, photo, _chat, moment = library(crm_db, tmp_path)
    face, second = uuid4(), uuid4()
    store_batch(FacesBatch.model_validate({"model": "m", "scans": [{"media_id": photo, "faces": [
        {"id": face, "box": [.1, .1, .2, .2], "quality": .9}, {"id": second, "box": [.5, .1, .2, .2], "quality": .8}]}],
        "suggestions": [{"face_id": face, "cluster_key": "k1", "profile_id": friend, "score": .7},
                        {"face_id": second, "cluster_key": "k1", "profile_id": None, "score": None}]}), crm_db)

    groups = review.groups(db=crm_db)["items"]
    assert [(g["key"], g["size"], g["person"] and g["person"]["name"]) for g in groups] == [
        (f"p:{friend}", 1, "Friend"), ("c:k1", 1, None)]
    assert groups[0]["faces"][0]["post_id"] == str(moment)

    previous = review.decide(Decision(face_ids=[face], action="confirm", profile_id=friend), crm_db)["previous"]
    assert previous[0]["status"] == "pending"
    tagged = review.in_media(ids=str(photo), db=crm_db)["faces"]
    assert [(f["name"], f["status"]) for f in tagged] == [("Friend", "confirmed")]
    seen = review.appearances(friend, db=crm_db)
    assert seen["total"] == 1 and seen["items"][0]["post_id"] == moment

    review.restore([FaceState.model_validate(p) for p in previous], crm_db)
    assert crm_db.execute(text("SELECT status FROM faces WHERE id = :id"), {"id": face}).scalar_one() == "pending"

    review.decide(Decision(face_ids=[face], action="not"), crm_db)
    store_batch(FacesBatch.model_validate({"model": "m", "suggestions": [
        {"face_id": face, "cluster_key": "k2", "profile_id": friend, "score": .8}]}), crm_db)
    row = crm_db.execute(text("SELECT suggested_profile_id, rejected_profile_id FROM faces WHERE id = :id"),
                         {"id": face}).one()
    assert row == (None, friend)
    review.decide(Decision(face_ids=[second], action="ignore"), crm_db)
    assert review.summary(db=crm_db)["to_review"] == 1
    assert other


def test_avatar_similarity_and_co_appearance_signals(crm_db, tmp_path):
    from app.faces.signals import avatar_matches, co_appearances
    friend, other, avatar, photo, _chat, _ = library(crm_db, tmp_path)
    on_avatar, a, b = uuid4(), uuid4(), uuid4()
    store_batch(FacesBatch.model_validate({"model": "m", "scans": [
        {"media_id": avatar, "faces": [{"id": on_avatar, "box": [.1, .1, .5, .5], "quality": .9}]},
        {"media_id": photo, "faces": [{"id": a, "box": [.1, .1, .2, .2], "quality": .9},
                                      {"id": b, "box": [.5, .1, .2, .2], "quality": .9}]}],
        "suggestions": [{"face_id": on_avatar, "cluster_key": None, "profile_id": other, "score": .55}]}), crm_db)
    assert avatar_matches(crm_db, {str(avatar)}) == {(str(avatar), str(other)): 0.55}

    review.decide(Decision(face_ids=[a], action="confirm", profile_id=friend), crm_db)
    review.decide(Decision(face_ids=[b], action="confirm", profile_id=other), crm_db)
    pair = co_appearances(crm_db)
    assert [(sorted([p["source"], p["target"]]), p["photos"]) for p in pair] == [(sorted([str(friend), str(other)]), 1)]


def test_review_paginates_groups_before_loading_ranked_face_samples(crm_db, tmp_path):
    friend, other, _avatar, photo, _chat, moment = library(crm_db, tmp_path)
    faces = []
    for profile, cluster, count in [(friend, "same-person", 22), (other, None, 2), (None, "large", 40)]:
        for n in range(count):
            faces.append({"id": uuid4(), "media": photo, "person": profile, "cluster": cluster, "score": n / count})
    for n in range(17):
        faces.append({"id": uuid4(), "media": photo, "person": None, "cluster": f"small-{n:02}", "score": None})
    crm_db.execute(text("""INSERT INTO faces(id, media_id, box, quality, in_post, suggested_profile_id, cluster_key, suggestion_score)
        VALUES(:id, :media, ARRAY[0.1, 0.1, 0.2, 0.2], 0.9, true, :person, :cluster, :score)"""), faces)

    items, offset = [], 0
    while True:
        page = review.groups(offset=offset, limit=2, db=crm_db)
        items.extend(page["items"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]

    assert [group["key"] for group in items] == [f"p:{friend}", f"p:{other}", "c:large"] + [
        f"c:small-{n:02}" for n in range(17)]
    assert [group["size"] for group in items[:3]] == [22, 2, 40]
    assert len(items[0]["faces"]) == len(items[2]["faces"]) == review.SAMPLES
    scores = [face["score"] for face in items[0]["faces"]]
    assert scores == sorted(scores, reverse=True)
    assert all(face["post_id"] == str(moment) for group in items for face in group["faces"])
    assert review.groups(offset=len(items), limit=2, db=crm_db) == {"items": [], "next_offset": None}


def test_transient_face_error_retries_but_zero_faces_is_success(crm_db, tmp_path):
    from app.faces.device import work
    _, _, avatar, photo, _, _ = library(crm_db, tmp_path)
    store_batch(FacesBatch.model_validate({'model': 'buffalo_l', 'scans': [
        {'media_id': avatar, 'faces': [], 'status': 'retryable_error', 'error': 'timeout'},
        {'media_id': photo, 'faces': [], 'status': 'success'},
    ]}), db=crm_db)
    assert not work(limit=10, db=crm_db)['items']
    crm_db.execute(text("UPDATE face_scans SET next_attempt_at=now()-interval '1 second' WHERE media_id=:id"), {'id': avatar})
    assert [str(row['media_id']) for row in work(limit=10, db=crm_db)['items']] == [str(avatar)]
    store_batch(FacesBatch.model_validate({'model': 'buffalo_l', 'scans': [
        {'media_id': avatar, 'faces': [], 'status': 'success'},
        {'media_id': photo, 'faces': [], 'model': 'buffalo_l:missing'},
    ]}), db=crm_db)
    assert not work(limit=10, db=crm_db)['items']
    assert set(crm_db.execute(text('SELECT status FROM face_scans')).scalars()) == {'success'}
