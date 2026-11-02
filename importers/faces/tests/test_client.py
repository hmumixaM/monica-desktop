import hashlib
import hmac
import io
import json
import tarfile

import httpx
import pytest

from monica_faces import client
from monica_faces.client import Monica
from monica_faces.config import Settings
from monica_faces.run import fetch


def tar(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, data in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return buffer.getvalue()


def monica_with(server) -> Monica:
    monica = Monica(Settings(secret="s3cret", token="tok", api="https://monica.test"))
    monica.http = httpx.Client(base_url="https://monica.test", transport=httpx.MockTransport(server))
    return monica


def test_requests_are_signed_over_method_target_and_body():
    seen = []

    def server(request: httpx.Request) -> httpx.Response:
        target = request.url.raw_path.decode()
        body = request.content
        message = b".".join([request.headers["x-import-timestamp"].encode(), request.headers["x-import-nonce"].encode(),
                             f"{request.method} {target}\n".encode() + body])
        expected = hmac.new(b"s3cret", message, hashlib.sha256).hexdigest()
        seen.append((request.method, target, request.headers["x-import-signature"] == expected,
                     request.headers["x-import-token"]))
        if request.url.path.endswith("/work"):
            return httpx.Response(200, json={"items": []})
        if request.url.path.endswith("/media"):
            return httpx.Response(200, content=tar({"abc": b"jpeg"}))
        return httpx.Response(200, json={"faces": 0})

    monica = monica_with(server)

    assert monica.work(5) == []
    assert monica.images(["abc", "gone"]) == {"abc": b"jpeg"}
    assert monica.upload({"model": "m", "scans": [{"media_id": "x", "faces": []}]}) == {"faces": 0}
    assert seen == [("GET", "/api/v1/faces/device/work?limit=5", True, "tok"),
                    ("POST", "/api/v1/faces/device/media", True, "tok"),
                    ("POST", "/api/v1/faces/device/batch", True, "tok")]


def test_a_stalled_batch_is_retried_image_by_image_and_only_the_stuck_one_is_skipped(monkeypatch):
    monkeypatch.setattr(client, "RETRY_PAUSE", 0)
    asked = []

    def server(request: httpx.Request) -> httpx.Response:
        ids = json.loads(request.content)["ids"]
        asked.append(ids)
        if len(ids) > 1 or ids == ["stuck"]:
            raise httpx.ReadTimeout("slow disk", request=request)
        return httpx.Response(200, content=tar({ids[0]: b"jpeg-" + ids[0].encode()}))

    images, stalled = fetch(monica_with(server), [{"media_id": m} for m in ("a", "stuck", "b")])

    assert images == {"a": b"jpeg-a", "b": b"jpeg-b"} and stalled == {"stuck"}
    assert asked[:client.IMAGE_TRIES] == [["a", "stuck", "b"]] * client.IMAGE_TRIES
    assert asked.count(["stuck"]) == client.IMAGE_TRIES


def test_a_server_error_is_retried_and_a_refusal_is_not(monkeypatch):
    monkeypatch.setattr(client, "RETRY_PAUSE", 0)
    replies = [httpx.Response(500), httpx.Response(200, content=tar({"a": b"jpeg"})), httpx.Response(401)]

    monica = monica_with(lambda request: replies.pop(0))

    assert monica.images(["a"]) == {"a": b"jpeg"}
    with pytest.raises(httpx.HTTPStatusError):
        monica.images(["a"])
    assert replies == []


def test_a_batch_whose_every_image_stalls_means_the_nas_is_stuck(monkeypatch):
    monkeypatch.setattr(client, "RETRY_PAUSE", 0)

    def server(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("stuck", request=request)

    with pytest.raises(httpx.ReadTimeout):
        fetch(monica_with(server), [{"media_id": str(i)} for i in range(5)])
