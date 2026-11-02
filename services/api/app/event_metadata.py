"""Small, source-backed event annotations for cards and event details."""
import re

from sqlalchemy import text


def story_extras(payload):
    """Text-only annotations. Never return artwork/audio URLs or infer filter names."""
    extras = []
    def add(kind, label, value, url=None):
        if not isinstance(value, str) or not value.strip():
            return
        item = {'kind': kind, 'label': label, 'text': value.strip()}
        if url:
            item['url'] = url
        if item not in extras:
            extras.append(item)

    for sticker in payload.get('story_music_stickers') or []:
        if not isinstance(sticker, dict):
            continue
        asset = sticker.get('music_asset_info') or {}
        parts = [asset.get('title'), asset.get('display_artist')]
        add('music', '背景音乐', ' · '.join(p for p in parts if isinstance(p, str) and p.strip()))
    for sticker in payload.get('story_feed_media') or []:
        if not isinstance(sticker, dict):
            continue
        code = sticker.get('media_code')
        url = f'https://www.instagram.com/p/{code}/' if isinstance(code, str) and re.fullmatch(r'[A-Za-z0-9_-]+', code) else None
        add('reshare', '转发帖子', '查看原帖' if url else '已收录转发贴纸', url)
    creative = payload.get('creative_config') or {}
    if isinstance(creative, dict):
        capture = creative.get('capture_type')
        if capture in ('boomerang', 'superzoom', 'layout'):
            add('effect', '拍摄效果', {'boomerang': 'Boomerang 循环', 'superzoom': '超级变焦', 'layout': '拼图'}[capture])
        creator = creative.get('attribution_user') or {}
        name = creator.get('username') if isinstance(creator, dict) else None
        if isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9_.]{1,30}', name):
            add('effect', '创作来源', '@' + name)
    mode = payload.get('create_mode_attribution') or {}
    if isinstance(mode, dict) and mode.get('name'):
        add('effect', '创作模式', '文字' if mode.get('type') == 'TYPE' else mode['name'])
    # Numeric filter codes have no reliable human-readable mapping in this archive.
    # Retain them in raw metadata rather than displaying an invented effect name.
    return extras


def normalize(provider, kind, payload):
    from .location_points import _location_label, _location_point

    payload = payload if isinstance(payload, dict) else {}
    places = payload.get('locations') or [payload.get('location')]
    if not isinstance(places, list):
        places = []
    locations = []
    for place in places:
        if not isinstance(place, dict) or not place:
            continue
        point = _location_point(place, 'chatlog_sns_xml' if payload.get('raw_xml_sha256') else None)
        label = _location_label(place)
        if not point and label == '未命名地点':
            continue
        entry = {'label': label, 'location_id': str(place.get('id') or place.get('pk') or '') or None, 'address': place.get('address'),
                 'coordinate_source': '地点贴纸' if kind == 'story' else '动态地点'}
        if point:
            lat, lon = point
            entry.update(latitude=lat, longitude=lon,
                         map_url=f'https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=16/{lat}/{lon}')
        if entry not in locations:
            locations.append(entry)
    mentions = []
    raw = (payload.get('archive_manifest') or {}).get('raw_metadata') or {}
    for container in (payload, raw, raw.get('iphone_struct') or {}):
        for item in container.get('reel_mentions') or []:
            if not isinstance(item, dict):
                continue
            user = item.get('user') or {}
            username = user.get('username')
            if not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9_.]{1,30}', username):
                continue
            entry = {'label': '@' + username, 'external_id': str(user.get('pk') or user.get('id') or ''),
                     'url': f'https://www.instagram.com/{username}/'}
            if entry not in mentions:
                mentions.append(entry)
    for item in payload.get('interactions') or []:
        if not isinstance(item, dict) or item.get('interaction_type') != 'mention' or item.get('is_deleted'):
            continue
        target = item.get('target_external_id')
        if target:
            entry = {'label': str(item.get('target_name') or target), 'external_id': str(target)}
            if entry not in mentions:
                mentions.append(entry)
    post = payload.get('instagram_post') if provider == 'instagram' and kind == 'post' else None
    if post:
        from .instagram_posts import user_identity
        caption_mentions=post.get('caption_mentions') or [{'username':name} for name in dict.fromkeys(re.findall(r'(?<![\w@])@([A-Za-z0-9_](?:[A-Za-z0-9_.]{0,28}[A-Za-z0-9_])?)',post.get('caption') or ''))]
        for item in caption_mentions:
            user=user_identity(item)
            if user:mentions.append({'label':'@'+user['username'],'external_id':user['external_id'],'url':user['url']})
        post.pop('caption',None)
    return {'event_type': kind, 'locations': locations, 'mentions': mentions, 'post': post, 'extras': story_extras(payload) if kind == 'story' else []}


def for_events(db, event_ids):
    if not event_ids:
        return {}
    rows = db.execute(text("""
        SELECT e.id, e.provider, e.kind, jsonb_build_object(
          'story_music_stickers', COALESCE(c.payload_json#>'{archive_manifest,raw_metadata,iphone_struct,story_music_stickers}', c.payload_json#>'{archive_manifest,effect_metadata,iphone_struct.story_music_stickers}'),
          'story_feed_media', COALESCE(c.payload_json#>'{archive_manifest,raw_metadata,iphone_struct,story_feed_media}', c.payload_json#>'{archive_manifest,effect_metadata,iphone_struct.story_feed_media}'),
          'creative_config', COALESCE(c.payload_json#>'{archive_manifest,raw_metadata,iphone_struct,creative_config}', c.payload_json#>'{archive_manifest,effect_metadata,iphone_struct.creative_config}'),
          'create_mode_attribution', COALESCE(c.payload_json#>'{archive_manifest,raw_metadata,iphone_struct,create_mode_attribution}', c.payload_json#>'{archive_manifest,effect_metadata,iphone_struct.create_mode_attribution}'),
          'instagram_post', CASE WHEN e.provider='instagram' AND e.kind='post' THEN jsonb_build_object(
            'post_id',COALESCE(NULLIF(c.payload_json->>'post_id',''),e.external_id),
            'shortcode',c.payload_json->'shortcode','permalink',c.payload_json->'permalink',
            'published_at',COALESCE(c.payload_json->>'published_at',e.occurred_at::text),
            'media_type',COALESCE(c.payload_json->>'media_type',CASE WHEN jsonb_array_length(COALESCE(c.payload_json->'media','[]'::jsonb))>1 THEN 'carousel' ELSE c.payload_json#>>'{media,0,media_type}' END),
            'tagged_users',c.payload_json->'tagged_users','caption_mentions',c.payload_json->'caption_mentions',
            'caption',COALESCE(c.payload_json->>'text',c.payload_json->>'caption')) END,
          'location', c.payload_json->'location', 'locations', c.payload_json->'locations',
          'raw_xml_sha256', c.payload_json->'raw_xml_sha256',
          'interactions', (SELECT jsonb_agg(i) FROM jsonb_array_elements(
              COALESCE(c.payload_json->'interactions','[]'::jsonb)) i WHERE i->>'interaction_type'='mention'),
          'reel_mentions', COALESCE(c.payload_json#>'{archive_manifest,raw_metadata,iphone_struct,reel_mentions}',
              c.payload_json#>'{archive_manifest,raw_metadata,reel_mentions}', c.payload_json->'reel_mentions')
        ) AS payload
        FROM (
          SELECT id,provider,content_object_id,external_id,occurred_at,'post' AS kind FROM social_posts WHERE id=ANY(:ids)
          UNION ALL
          SELECT id,provider,content_object_id,external_id,occurred_at,'story' AS kind FROM social_stories WHERE id=ANY(:ids)
        ) e JOIN content_objects c ON c.id=e.content_object_id
    """), {'ids': event_ids}).mappings()
    result = {str(row['id']): normalize(row['provider'], row['kind'], row['payload']) for row in rows}
    # Exact existing identity matches only; a mention never creates or merges people.
    targets = list({m['external_id'] for value in result.values() for m in value['mentions'] if 'url' not in m})
    if targets:
        names = {row['external_id']: row for row in db.execute(text("""
            SELECT i.external_id,p.id,p.display_name FROM identities i JOIN profiles p ON p.id=i.profile_id
            WHERE i.provider='wechat' AND i.external_id=ANY(:targets)
        """), {'targets': targets}).mappings()}
        for value in result.values():
            for mention in value['mentions']:
                person = names.get(mention['external_id']) if 'url' not in mention else None
                if person:
                    mention.update(label=person['display_name'], url=f"/profiles/{person['id']}")
    return result
