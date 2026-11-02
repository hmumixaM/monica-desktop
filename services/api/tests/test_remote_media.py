import hashlib
import importlib.util
from email.message import Message
from io import BytesIO
from pathlib import Path

import pytest

from app.remote_media import download_image, linkedin_image_url, merge_media


def test_wechat_cache_url_preserves_source_protocol():
    from app.archive_media import fix_url

    assert (
        fix_url("http://media.test/photo/150?quality=1", "token", video=False)
        == "http://media.test/photo/0?quality=1&token=token&idx=1"
    )
    assert (
        fix_url("http://media.test/video?quality=1", "token", video=True)
        == "http://media.test/video?token=token&idx=1&quality=1"
    )


def test_only_linkedin_cdn_can_be_downloaded():
    assert linkedin_image_url("https://media.licdn.com/dms/image/test")
    for url in ("http://media.licdn.com/a", "https://127.0.0.1/a", "https://media.licdn.com.evil.test/a",
                "https://user:pass@media.licdn.com/a", "file:///etc/passwd"):
        assert linkedin_image_url(url) is None


def test_download_is_bounded_atomic_and_content_addressed(tmp_path, monkeypatch):
    payload = b"test-image-bytes"

    class Response(BytesIO):
        headers = Message()
        headers["Content-Type"] = "image/jpeg"

    class Opener:
        def open(self, *_args, **_kwargs):
            return Response(payload)

    monkeypatch.setattr("urllib.request.build_opener", lambda *_args: Opener())
    first = download_image("https://media.licdn.com/a", tmp_path)
    second = download_image("https://media.licdn.com/b", tmp_path)
    assert first == second
    assert first[0] == hashlib.sha256(payload).hexdigest()
    assert Path(first[1]).read_bytes() == payload
    assert len(list((tmp_path / "sha256").rglob("*"))) == 3
    with pytest.raises(ValueError, match="limit"):
        download_image("https://media.licdn.com/c", tmp_path, max_bytes=2)
    assert list((tmp_path / ".staging").iterdir()) == []


def test_timeline_prefers_local_media_and_preserves_pending_media():
    result = merge_media([
        {"source_url": "https://media.licdn.com/a", "role": "media-1"},
        {"source_url": "https://media.licdn.com/b", "role": "media-2"},
    ], [{"source_url": "https://media.licdn.com/a", "id": "asset-1", "role": "media-1"}])
    assert len(result) == 2
    assert result[0]["id"] == "asset-1"
    assert result[1]["source_url"].endswith("/b")


def test_collector_avatar_belongs_to_target_and_uses_largest_photo():
    path = Path(__file__).parents[3] / "infra/linkedin/profile_media.py"
    spec = importlib.util.spec_from_file_location("profile_media", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    raw = {"included": [
        {"publicIdentifier": "someone-else", "picture": {
            "rootUrl": "https://media.licdn.com/", "artifacts": [
                {"width": 1000, "fileIdentifyingUrlPathSegment": "wrong.jpg"}]}},
        {"publicIdentifier": "target", "profilePicture": {"displayImageReference": {
            "vectorImage": {"rootUrl": "https://media.licdn.com/", "artifacts": [
                {"width": 100, "fileIdentifyingUrlPathSegment": "small.jpg"},
                {"width": 800, "fileIdentifyingUrlPathSegment": "large.jpg"}]}}}},
    ]}
    assert module.profile_avatar(raw, "target") == "https://media.licdn.com/large.jpg"
    assert module.profile_avatar(raw, "missing") is None


def test_cached_video_poster_is_local_until_original_arrives():
    remote=[{'source_url':'https://cdninstagram.com/video','thumbnail_url':'https://cdninstagram.com/poster','media_type':'video'}]
    poster={'id':'poster','source_url':remote[0]['thumbnail_url'],'media_type':'image/jpeg'}
    video={'id':'video','source_url':remote[0]['source_url'],'media_type':'video/mp4'}
    assert merge_media(remote,[poster])==[{**remote[0],**poster,'source_media_type':'video'}]
    assert merge_media(remote,[poster,video])==[{**remote[0],**video,'thumbnail_id':'poster','source_media_type':'video'}]


def test_archive_cli_signal_reports_incomplete_scan():
    import subprocess
    import sys

    code="""
import os,signal,sys
from app import archive_media

def interrupted(*args):
    os.kill(os.getpid(),signal.SIGTERM)
    assert args[-1]()
    return {'stopped_at_checkpoint':True}
archive_media.moments_cache=interrupted
sys.argv=['archive_media','--sns-db','unused','--cache','unused']
archive_media.main()
"""
    result=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,timeout=15,check=False)
    assert result.returncode==75,result.stderr
    assert '"stopped_at_checkpoint": true' in result.stdout


def test_channels_cover_cdn_allowlist():
    from app.remote_media import provider_image_url
    assert provider_image_url('https://wxapp.tc.qq.com/251/20304/stodownload?encfilekey=test')
    assert not provider_image_url('https://wxapp.tc.qq.com.evil.test/image.jpg')
    assert not provider_image_url('http://wxapp.tc.qq.com/image.jpg')


def test_channels_covers_accept_only_exact_trusted_https_hosts():
    from app.remote_media import provider_image_url

    for host in ('wxapp.tc.qq.com', 'finder.video.qq.com'):
        url = f'https://{host}/cover?signature=example'
        assert provider_image_url(url) == url
    for url in (
        'http://finder.video.qq.com/cover',
        'https://finder.video.qq.com.evil.test/cover',
        'https://evil.finder.video.qq.com/cover',
        'https://user:password@finder.video.qq.com/cover',
        'https://finder.video.qq.com:8443/cover',
        'https://127.0.0.1/cover',
    ):
        assert provider_image_url(url) is None
