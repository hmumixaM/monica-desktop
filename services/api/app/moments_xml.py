"""Read legacy Moments XML without discarding illegal numeric references.

Some collector exports contain XML 1.0 control-character references. Preserve
those exact references as visible literal text; never strip them or repair the
XML structure. Callers retain the original source hash and repair counts.
"""
import re
import xml.etree.ElementTree as ET
from collections import Counter


def parse_moments_xml(value):
    try:
        return ET.fromstring(value or ''), {}
    except ET.ParseError:
        repairs = Counter()

        def escape(match):
            token = match.group(1)
            if token is None:
                return match.group(0)  # CDATA, comments and processing instructions
            digits = token.removeprefix('x')
            digits = digits.lstrip('0') or '0'
            number = int(digits, 16 if token.startswith('x') else 10) if len(digits) <= 8 else 0x110000
            if number in (9, 10, 13) or 0x20 <= number <= 0xD7FF or 0xE000 <= number <= 0xFFFD or 0x10000 <= number <= 0x10FFFF:
                return match.group(0)
            repairs[match.group(0)] += 1
            return '&amp;' + match.group(0)[1:]

        escaped = re.sub(r'<!\[CDATA\[.*?\]\]>|<!--.*?-->|<\?.*?\?>|&#(x[0-9a-fA-F]+|[0-9]+);', escape, value or '', flags=re.DOTALL)
        # Malformed tags, unknown entities and other corruption still fail.
        return ET.fromstring(escaped), dict(repairs)


def shared_link(obj):
    """Extract the shared destination, never confuse its cover with the post."""
    from urllib.parse import urlsplit

    content = obj.find('ContentObject')
    if content is None or content.findtext("type") != "3":
        return None
    url = (content.findtext('contentUrl') or '').strip()
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            return None
    except ValueError:
        return None
    return {'url': url, 'title': (content.findtext('title') or '').strip(),
            'description': (content.findtext('description') or '').strip(),
            'source_name': (obj.findtext('sourceNickName') or '').strip()}


def finder_media(obj):
    """Keep Channels metadata and queue only its poster, never an unverified video."""
    from urllib.parse import urlsplit

    feed = obj.find('ContentObject/finderFeed')
    if feed is None:
        return []

    def number(value):
        try:
            result = float(value or 0)
            return int(result) if 0 < result < 10000000 else None
        except (ValueError, OverflowError):
            return None

    rows = []
    for index, media in enumerate(feed.findall('mediaList/media'), 1):
        cover = media.findtext('coverUrl') or media.findtext('thumbUrl') or ''
        try:
            parsed = urlsplit(cover)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
                cover = None
        except ValueError:
            cover = None
        duration = number(media.findtext('videoPlayDuration'))
        rows.append({'role': f'media-{index}', 'media_type': 'video',
                     'source_url': None, 'thumbnail_url': cover,
                     'source_kind': 'wechat_channels', 'source_media_id': feed.findtext('objectId'),
                     'author_name': feed.findtext('nickname'), 'description': feed.findtext('desc'),
                     'duration_ms': duration * 1000 if duration else None,
                     'width': number(media.findtext('width')), 'height': number(media.findtext('height'))})
    return rows


def enqueue_finder_covers(db, post_id, media):
    from .remote_media import enqueue_media

    for index, item in enumerate(media, 1):
        if item.get('source_kind') == 'wechat_channels' and item.get('thumbnail_url'):
            enqueue_media(db, url=item['thumbnail_url'], entity_type='social_post',
                          entity_id=post_id, role=f'thumbnail-{index}')
