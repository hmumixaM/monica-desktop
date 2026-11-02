import xml.etree.ElementTree as ET

import pytest

from app.moments_xml import parse_moments_xml


def test_illegal_references_preserve_literal_evidence_and_valid_unicode():
    root, repairs = parse_moments_xml('<root><text>A&#3;B&#x19;&#x1F600;&#10;</text><raw><![CDATA[&#3;]]></raw><!-- &#8; --></root>')
    assert root.findtext('text') == 'A&#3;B&#x19;😀\n'
    assert root.findtext('raw') == '&#3;'
    assert repairs == {'&#3;': 1, '&#x19;': 1}
    normal, repairs = parse_moments_xml('<root>A &amp; B &#x1F600;</root>')
    assert normal.text == 'A & B 😀' and repairs == {}
    with pytest.raises(ET.ParseError):
        parse_moments_xml('<root>&#3;<broken></root>')
    with pytest.raises(ET.ParseError):
        parse_moments_xml('<root>&unknown;</root>')


def test_shared_article_preserves_title_destination_and_publisher_without_cover():
    from app.moments_xml import shared_link
    obj = ET.fromstring('<TimelineObject><sourceNickName>鉴茶院</sourceNickName><ContentObject><type>3</type><title>文章标题</title><description>摘要</description><contentUrl>https://mp.weixin.qq.com/s?a=1&amp;b=2</contentUrl><mediaList/></ContentObject></TimelineObject>')
    assert shared_link(obj) == {'url': 'https://mp.weixin.qq.com/s?a=1&b=2', 'title': '文章标题', 'description': '摘要', 'source_name': '鉴茶院'}
    assert shared_link(ET.fromstring('<TimelineObject><ContentObject><type>1</type></ContentObject></TimelineObject>')) is None
    for url in ('javascript:alert(1)', 'file:///etc/passwd', '//example.test', 'https://user:pass@example.test', 'https://['):
        obj.find('ContentObject/contentUrl').text = url
        assert shared_link(obj) is None


def test_video_upgrade_url_is_not_a_shared_article():
    from app.moments_xml import shared_link
    obj = ET.fromstring('<TimelineObject><ContentObject><type>15</type><title>Weixin Sight</title><contentUrl>https://support.weixin.qq.com/upgrade</contentUrl></ContentObject></TimelineObject>')
    assert shared_link(obj) is None


def test_channels_poster_metadata_does_not_queue_video_or_update_link():
    from app.moments_xml import finder_media, shared_link
    obj = ET.fromstring('''<TimelineObject><ContentObject><type>28</type>
    <contentUrl>https://support.weixin.qq.com/update/</contentUrl><finderFeed>
    <objectId>123</objectId><nickname>作者</nickname><desc>标题</desc><mediaList><media>
    <url>https://example.test/expired-video</url><coverUrl>https://example.test/cover.jpg</coverUrl>
    <videoPlayDuration>12</videoPlayDuration><width>720</width><height>1280</height>
    </media></mediaList></finderFeed></ContentObject></TimelineObject>''')
    row = finder_media(obj)[0]
    assert row['thumbnail_url'] == 'https://example.test/cover.jpg'
    assert row['source_url'] is None and row['media_type'] == 'video'
    assert (row['author_name'], row['description'], row['duration_ms']) == ('作者','标题',12000)
    assert shared_link(obj) is None
    obj.find('.//coverUrl').text = 'javascript:alert(1)'
    obj.find('.//videoPlayDuration').text = 'NaN'
    assert finder_media(obj)[0]['thumbnail_url'] is None
    assert finder_media(obj)[0]['duration_ms'] is None


def test_channels_photo_style_share_keeps_distinct_direct_image_slot():
    from app.moments_posts import moments_media as _moments_media
    obj = ET.fromstring('''<TimelineObject><ContentObject><type>7</type>
    <finderFeed><nickname>作者</nickname><mediaList><media><coverUrl>https://wxapp.tc.qq.com/cover</coverUrl></media></mediaList></finderFeed>
    <mediaList><media><type>2</type><url>https://example.test/original.jpg</url></media></mediaList>
    </ContentObject></TimelineObject>''')
    rows = _moments_media(obj)
    assert [r['role'] for r in rows] == ['media-1','media-2']
    assert rows[0]['source_kind'] == 'wechat_channels'
    assert rows[1]['source_url'] == 'https://example.test/original.jpg'
