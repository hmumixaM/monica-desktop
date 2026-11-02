from uuid import uuid4

from sqlalchemy import text
from test_account_links import source_engine as source_engine  # noqa: PLC0414
from test_instagram_stories import fixture_archive
from test_media_integration import db as db  # noqa: PLC0414

from app.event_metadata import normalize


def test_annotations_use_mention_target_and_source_coordinate_format():
    metadata = normalize('wechat', 'post', {
        'raw_xml_sha256': 'source-hash',
        'location': {'poiName': 'Paris', 'latitude': 2.35, 'longitude': 48.85},
        'interactions': [
            {'interaction_type': 'mention', 'author_external_id': 'owner', 'target_external_id': 'friend'},
            {'interaction_type': 'mention', 'target_external_id': 'deleted', 'is_deleted': True},
            {'interaction_type': 'like', 'author_external_id': 'liker'},
        ],
    })
    assert normalize('wechat', 'post', {'location': {'latitude': '0', 'longitude': '0', 'poiScale': '0'}})['locations'] == []
    assert metadata['locations'][0]['latitude'] == 48.85
    assert metadata['locations'][0]['longitude'] == 2.35
    assert metadata['mentions'] == [{'label': 'friend', 'external_id': 'friend'}]
    assert 'map_url' not in normalize('instagram', 'post', {'location': {'name': 'Place', 'lat': 1000, 'lng': 3}})['locations'][0]


def test_story_card_and_detail_share_location_and_mentions_without_raw_archive(db, source_engine, tmp_path):
    import json

    from app.routers.timeline import timeline, timeline_detail
    from app.sync_instagram_stories import cycle
    db.execute(text("INSERT INTO platform_migration_state(provider,enabled) VALUES('instagram',true)")); db.commit()
    root, path, doc = fixture_archive(tmp_path)
    doc['raw_metadata']['iphone_struct'] = {
        'story_music_stickers': [{'music_asset_info': {'title': 'Bad Angel', 'display_artist': 'Anyma, LISA', 'progressive_download_url': 'https://external.test/audio', 'cover_artwork_uri': 'https://external.test/cover'}}],
        'story_feed_media': [{'media_code': 'AbC_123'}],
        'creative_config': {'capture_type': 'boomerang', 'attribution_user': {'username': 'instagram'}},
        'story_locations': [{'x': .2, 'y': .3, 'location': {'name': 'Restaurant Daniel', 'lat': 40.76678, 'lng': -73.96757}}],
        'reel_mentions': [{'user': {'username': 'real_account', 'pk': 123}}, {'user': {'username': 'bad/../../name'}}],
    }
    path.write_text(json.dumps(doc))
    cycle(db, root, tmp_path/'media', tmp_path/'state.json')
    rows = timeline(db=db, _user={})
    assert len(rows) == 1
    metadata = rows[0]['event_metadata']
    assert metadata['event_type'] == 'story'
    assert metadata['extras'][0] == {'kind': 'music', 'label': '背景音乐', 'text': 'Bad Angel · Anyma, LISA'}
    assert metadata['extras'][1]['url'] == 'https://www.instagram.com/p/AbC_123/'
    assert metadata['extras'][2]['text'] == 'Boomerang 循环'
    assert 'external.test' not in json.dumps(metadata)
    assert metadata['mentions'] == [{'label': '@real_account', 'external_id': '123', 'url': 'https://www.instagram.com/real_account/'}]
    assert metadata['locations'][0]['label'] == 'Restaurant Daniel'
    assert metadata['locations'][0]['latitude'] == 40.76678
    assert 'archive_manifest' not in json.dumps(rows[0], default=str)
    assert timeline_detail(rows[0]['id'], db=db, _user={})['event_metadata'] == metadata
    assert timeline_detail(rows[0]['id'], db=db, _user={})['content']['archive_manifest']['raw_metadata'] == doc['raw_metadata']


def test_manual_activity_does_not_gain_social_metadata(db):
    from app.routers.timeline import timeline
    db.execute(text("INSERT INTO activities(id,title,activity_type,occurred_at) VALUES(:id,'Dinner','note',now())"), {'id': uuid4()})
    rows = timeline(db=db, _user={})
    assert rows[0]['event_metadata'] is None


def test_story_extras_ignore_unknown_codes_and_unsafe_links():
    from app.event_metadata import story_extras
    assert story_extras({'filter_type': 810}) == []
    result = story_extras({'story_music_stickers': [None, {'music_asset_info': {}}],
                          'story_feed_media': [{'media_code': '../../evil'}],
                          'creative_config': {'attribution_user': {'username': 'bad/name'}}})
    assert result == [{'kind': 'reshare', 'label': '转发帖子', 'text': '已收录转发贴纸'}]
