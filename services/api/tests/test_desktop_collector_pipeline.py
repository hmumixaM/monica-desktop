"""Identical collected fixtures pass through desktop publication and NAS importers."""
import json
from pathlib import Path
from uuid import uuid4

import pytest
from PIL import Image
from sqlalchemy import text
from test_desktop import desktop_settings
from test_desktop import desktop_stores as desktop_stores  # noqa: PLC0414


@pytest.mark.parametrize("mode", ["development", "desktop"])
def test_published_instagram_posts_stories_highlights_match_server_semantics(crm_db, desktop_stores, tmp_path, monkeypatch, mode):
    from app.archive_media import instagram_archive
    from app.config import get_settings
    from app.desktop_init import initialize
    from app.platform_migrate import register_accounts
    from app.reconcile_sources import instagram, instagram_profiles
    from app.sync_instagram_stories import cycle, highlight_cycle

    monkeypatch.syspath_prepend(str(Path(__file__).parents[3] / "apps/desktop/collectors"))
    import publishing
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path / "media"))
    desktop_settings(monkeypatch)
    initialize(crm_db)
    monkeypatch.setenv("APP_ENV", mode)
    get_settings.cache_clear()
    person = uuid4()
    crm_db.execute(text("INSERT INTO profiles(id,profile_type,display_name) VALUES(:id,'person','Fixture')"), {'id':person})
    for identifier in ('1001', 'username:fixture'):
        crm_db.execute(text("""INSERT INTO identities(provider,external_id,profile_id,username)
            VALUES('instagram',:external,:profile,'fixture')"""), {'external':identifier,'profile':person})
    register_accounts(crm_db, 'instagram')
    root = tmp_path / "collector"
    profile = root / "data/profiles/fixture"
    profile.mkdir(parents=True)
    (profile / "profile.json").write_text(json.dumps({'userid':'1001','username':'fixture','full_name':'Fixture','biography':'Collected bio'}))
    post = {'id':'2001','owner':{'id':'1001','username':'fixture'},'shortcode':'FIX','date':1700000000,
            'caption':'Collected @friend','display_url':'https://scontent.cdninstagram.com/post.jpg',
            'location':{'pk':3,'name':'Fixture place','lat':40.7,'lng':-74.0}}
    metadata = profile / "2023_UTC_FIX.json"
    metadata.write_text(json.dumps({'node':post}))
    Image.new('RGB',(12,8),'blue').save(profile / '2023_UTC_FIX.jpg')
    relationship = {'shortcode':'FIX','comments':{'policy':'collected','records':[
        {'id':77,'text':'Fixture comment','owner':{'userid':9,'username':'friend'}}]}}
    (profile / 'post-FIX.relationships.json').write_text(json.dumps(relationship))
    for kind, folder in (('stories', root/'data/stories/fixture'), ('highlights', root/'data/highlights/fixture/3003')):
        folder.mkdir(parents=True)
        (folder / 'item.jpg').write_bytes((profile / '2023_UTC_FIX.jpg').read_bytes())
        doc = {'schema_version':1,'mediaid':'2002','owner_id':'1001','owner_username':'fixture','files':['item.jpg'],
               'captured_at':1700000000,'taken_at':'2023-11-14T00:00:00Z','expiring_at':'2023-11-15T00:00:00Z',
               'caption':'Collected story','is_video':False,'raw_metadata':{}}
        if kind == 'highlights':doc.update(archive_kind='highlight',highlight_id='3003',highlight_title='Saved')
        (folder / 'item.manifest.json').write_text(json.dumps(doc))
    publishing.instagram(root, tmp_path)
    profiles = (tmp_path/'staging/instagram-current/CURRENT').resolve()/'profiles'
    media = tmp_path/'media'
    def import_all():
        instagram_profiles(crm_db, profiles, apply=True)
        report = instagram(crm_db, profiles, apply=True)
        instagram_archive(crm_db, profiles, media)
        cycle(crm_db, tmp_path/'staging/instagram-stories', media, tmp_path/'stories-checkpoint.json')
        highlight_cycle(crm_db, tmp_path/'staging/instagram-highlights', media, tmp_path/'highlights-checkpoint.json')
        return report
    assert import_all()['differences'] == 1
    row = crm_db.execute(text("""SELECT p.profile_id,p.occurred_at,o.payload_json FROM social_posts p
        JOIN content_objects o ON o.id=p.content_object_id""")).one()
    assert row.profile_id == person and row.occurred_at.year == 2023
    assert row.payload_json['text'] == 'Collected @friend'
    assert row.payload_json['location']['lat'] == 40.7
    assert row.payload_json['interactions'][0]['content']['text'] == 'Fixture comment'
    assert set(crm_db.execute(text('SELECT external_id FROM social_stories')).scalars()) == {'2002','highlight:3003:2002'}
    assert crm_db.execute(text('SELECT count(*) FROM media_assets')).scalar_one() == 1
    assert crm_db.execute(text("SELECT count(*) FROM media_links WHERE entity_type IN ('social_post','social_story')")).scalar_one() == 3
    assert import_all()['differences'] == 0
    assert crm_db.execute(text('SELECT count(*) FROM social_posts')).scalar_one() == 1
    assert crm_db.execute(text('SELECT count(*) FROM social_stories')).scalar_one() == 2
    post['caption'] = 'Updated caption'
    metadata.write_text(json.dumps({'node':post}))
    publishing.instagram(root, tmp_path, kinds=('profiles',))
    profiles = (tmp_path/'staging/instagram-current/CURRENT').resolve()/'profiles'
    assert import_all()['differences'] == 1
    assert crm_db.execute(text('SELECT count(*) FROM social_posts')).scalar_one() == 1
    assert crm_db.execute(text('SELECT count(*) FROM profiles')).scalar_one() == 1
