from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event, text
from test_faces import link, post
from test_ui_endpoints import account, media, person

from app.faces import photos, review
from app.faces.device import FacesBatch, anchors, batch


def face(db, who, image, state='pending', score=.75):
    fid = uuid4()
    db.execute(text("""INSERT INTO faces(id,media_id,box,quality,status,profile_id,suggested_profile_id,suggestion_score)
        VALUES(:id,:m,ARRAY[0.1,0.1,0.3,0.3],0.9,:s,:p,:candidate,:score)"""),
        {'id': fid, 'm': image, 's': state, 'p': who if state == 'confirmed' else None,
         'candidate': who if state == 'pending' else None, 'score': score})
    return fid


def test_gallery_sources_filters_counts_and_feedback(crm_db):
    db = crm_db
    who, other = person(db, 'Gallery person'), person(db, 'Other person')
    public = account(db, 'gh_official')
    ordinary = post(db, other, 'ordinary')
    official = post(db, other, 'official')
    db.execute(text('UPDATE social_posts SET source_account_id=:a WHERE id=:p'), {'a': public, 'p': official})
    story = db.execute(text("""INSERT INTO social_stories(provider,external_id,profile_id,occurred_at,current_content_hash)
        VALUES('instagram','story',:p,'2026-10-10',:h) RETURNING id"""), {'p': other, 'h': 's' * 64}).scalar_one()
    conversation = db.execute(text("INSERT INTO conversations(conversation_type,external_id,profile_id) VALUES('group','group',:p) RETURNING id"), {'p': other}).scalar_one()
    message = uuid4()
    db.execute(text("""INSERT INTO message_events(id,conversation_id,occurred_at,message_type,source_record_id)
        VALUES(:id,:c,'2026-10-11','3','photo-message')"""), {'id': message, 'c': conversation})
    images = [media(db, str(i)) for i in range(6)]
    link(db, images[0], 'social_post', ordinary, 'media-1')
    link(db, images[0], 'social_story', story, 'media-1')
    link(db, images[0], 'social_post', official, 'media-2')
    link(db, images[1], 'message_event', message, 'chat_image')
    link(db, images[2], 'social_post', official, 'media-1')
    link(db, images[3], 'social_post', uuid4(), 'media-1')  # deleted source
    ids = [face(db, who, image) for image in images]
    face(db, who, images[0], 'confirmed')
    db.execute(text("UPDATE faces SET status='ignored' WHERE id=:id"), {'id': ids[4]})
    db.execute(text('UPDATE faces SET rejected_profile_id=:p WHERE id=:id'), {'p': who, 'id': ids[5]})
    db.commit()

    page = photos.photos(who, db=db)
    assert page['counts'] == {'all': 3, 'pending': 3, 'confirmed': 1}
    assert {r['media_id'] for r in page['items']} == {images[0], images[1], images[3]}
    shared = next(r for r in page['items'] if r['media_id'] == images[0])
    assert len(shared['faces']) == 2
    assert {s['entity_type'] for s in shared['sources']} == {'social_post', 'social_story'}
    assert all(str(official) not in s['href'] for s in shared['sources'])
    chat = next(r for r in page['items'] if r['media_id'] == images[1])
    assert f'message={message}' in chat['sources'][0]['href']
    orphan = next(r for r in page['items'] if r['media_id'] == images[3])
    assert orphan['sources'][0]['href'] is None
    assert len(photos.photos(who, state='confirmed', db=db)['items']) == 1
    assert len(photos.photos(who, media_id=images[1], db=db)['items']) == 1

    before = review.decide(review.Decision(face_ids=[ids[1]], action='confirm', profile_id=who), db)['previous']
    assert photos.counts(db, who)['confirmed'] == 2
    assert any(a['face_id'] == ids[1] for a in anchors(db)['anchors'])
    batch(FacesBatch(model='synthetic', suggestions=[{'face_id': ids[1], 'profile_id': other, 'score': .9, 'cluster_key': None}]), db)
    assert db.execute(text('SELECT profile_id FROM faces WHERE id=:id'), {'id': ids[1]}).scalar_one() == who
    review.restore([review.FaceState.model_validate(s) for s in before], db)
    assert photos.counts(db, who)['confirmed'] == 1
    review.decide(review.Decision(face_ids=[ids[1]], action='not', profile_id=who), db)
    assert photos.counts(db, who)['all'] == 2
    review.decide(review.Decision(face_ids=[ids[1]], action='confirm', profile_id=other), db)
    assert photos.counts(db, other)['confirmed'] == 1


def test_cursor_pagination_null_scores_new_records_and_cached_counts(crm_db):
    db = crm_db
    who = person(db, 'Paging')
    for i, score in enumerate([.9, .8, None, None]):
        image = media(db, f'page-{i}')
        db.execute(text("UPDATE media_assets SET created_at='2026-01-01' WHERE id=:id"), {'id': image})
        face(db, who, image, score=score)
    db.commit()
    statements = []

    def capture(_conn, _cursor, statement, *_args):
        statements.append(statement)

    event.listen(db.bind, 'before_cursor_execute', capture)
    try:
        first = photos.photos(who, sort='score', limit=2, db=db)
        second = photos.photos(who, sort='score', limit=2, cursor=first['next_cursor'], db=db)
        assert second['next_cursor'] is None
        assert [r['score'] for r in first['items']] == pytest.approx([.9, .8])
        assert [r['score'] for r in second['items']] == [None, None]
        assert len([s for s in statements if 'count(*) AS all' in s]) == 1
    finally:
        event.remove(db.bind, 'before_cursor_execute', capture)
    first = photos.photos(who, limit=2, db=db)
    newest = media(db, 'newly-imported')
    face(db, who, newest)
    db.commit()
    second = photos.photos(who, limit=2, cursor=first['next_cursor'], db=db)
    assert len({r['media_id'] for r in first['items'] + second['items']}) == 4
    assert newest not in [r['media_id'] for r in second['items']]
    assert photos.photos(who, db=db)['items'][0]['media_id'] == newest
    for cursor in ['bad', first['next_cursor']]:
        with pytest.raises(HTTPException) as error:
            photos.photos(who, state='confirmed', cursor=cursor, db=db)
        assert error.value.status_code == 422


def test_preview_cached_oriented_and_original_unchanged(crm_db, tmp_path):
    from PIL import Image
    from starlette.requests import Request

    from app.photo_preview import preview
    image = media(crm_db, 'preview')
    original = tmp_path / 'original.jpg'
    exif = Image.Exif()
    exif[274] = 6
    Image.new('RGB', (1000, 600), 'blue').save(original, exif=exif)
    content = original.read_bytes()
    crm_db.execute(text('UPDATE media_assets SET object_path=:path WHERE id=:id'), {'path': str(original), 'id': image})
    crm_db.commit()
    response = preview(image, Request({'type': 'http', 'headers': []}), crm_db)
    with Image.open(response.path) as result:
        assert result.size == (288, 480)
    assert original.read_bytes() == content
    cached = preview(image, Request({'type': 'http', 'headers': [(b'if-none-match', response.headers['etag'].encode())]}), crm_db)
    assert cached.status_code == 304


def test_face_preview_uses_saved_box_and_distinct_oriented_cache(crm_db, tmp_path):
    from PIL import Image
    from starlette.requests import Request

    from app.photo_preview import preview

    who = person(crm_db, 'Crop fixture')
    image = media(crm_db, 'face-crops')
    first, second = face(crm_db, who, image), face(crm_db, who, image)
    # EXIF rotation puts the original red left half above the blue half.
    original = tmp_path / 'oriented-faces.jpg'
    picture = Image.new('RGB', (1200, 800), 'red')
    picture.paste('blue', (600, 0, 1200, 800))
    exif = Image.Exif()
    exif[274] = 6
    picture.save(original, exif=exif)
    content = original.read_bytes()
    crm_db.execute(text('UPDATE media_assets SET object_path=:path WHERE id=:id'), {'path': str(original), 'id': image})
    crm_db.execute(text('UPDATE faces SET box=ARRAY[0.3,0.1,0.2,0.2] WHERE id=:id'), {'id': first})
    crm_db.execute(text('UPDATE faces SET box=ARRAY[0.3,0.7,0.2,0.2] WHERE id=:id'), {'id': second})
    crm_db.commit()
    request = Request({'type': 'http', 'headers': []})
    red = preview(image, request, crm_db, face_id=first)
    blue = preview(image, request, crm_db, face_id=second)
    assert red.path != blue.path
    assert red.headers['etag'] != blue.headers['etag']
    for response, channel in [(red, 0), (blue, 2)]:
        with Image.open(response.path) as result:
            assert result.size == (192, 192)
            assert result.getpixel((96, 96))[channel] > 240
    cached = preview(image, Request({'type': 'http', 'headers': [(b'if-none-match', red.headers['etag'].encode())]}),
                     crm_db, face_id=first)
    assert cached.status_code == 304
    assert original.read_bytes() == content
    another = media(crm_db, 'other-photo')
    with pytest.raises(HTTPException) as error:
        preview(another, request, crm_db, face_id=first)
    assert error.value.status_code == 404
    crm_db.execute(text('UPDATE faces SET box=ARRAY[0.3,0.7,0.2,0.2] WHERE id=:id'), {'id': first})
    crm_db.commit()
    assert preview(image, request, crm_db, face_id=first).headers['etag'] != red.headers['etag']
    crm_db.execute(text('UPDATE faces SET box=ARRAY[10.0,10.0,0.1,0.1] WHERE id=:id'), {'id': first})
    crm_db.commit()
    with pytest.raises(HTTPException) as error:
        preview(image, request, crm_db, face_id=first)
    assert error.value.status_code == 404


def test_chat_source_opens_at_target_message(crm_db):
    from datetime import UTC, datetime, timedelta

    from test_ui_endpoints import chat

    from app.routers.messages import conversation_messages

    who = person(crm_db, 'Chat source')
    now = datetime(2026, 10, 16, tzinfo=UTC)
    cid = chat(crm_db, who, 'wx_photo_source', [(now - timedelta(minutes=i), False, str(i)) for i in range(70)])
    anchor = crm_db.execute(text("SELECT message_id FROM message_search WHERE body='60'")).scalar_one()
    rows = conversation_messages(cid, message=anchor, db=crm_db)
    assert rows[0]['id'] == anchor
    assert len(rows) == 10
    another = chat(crm_db, who, 'wx_another', [(now, False, 'different conversation')])
    assert conversation_messages(another, message=anchor, db=crm_db) == []


def test_photo_confirmations_do_not_create_relationship_graph_edges(crm_db, tmp_path):
    from app.graph import graph_projection
    from app.orbit import communities

    who, other = person(crm_db, 'A'), person(crm_db, 'B')
    image = media(crm_db, 'co-photo')
    face(crm_db, who, image, 'confirmed')
    face(crm_db, other, image, 'confirmed')
    graph = graph_projection(crm_db, tmp_path)
    assert not graph['edges']
    assignment, _ = communities(crm_db, [str(who), str(other)])
    assert assignment[str(who)] == assignment[str(other)] == 'other'
