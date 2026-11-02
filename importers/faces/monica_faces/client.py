"""Signed requests to Monica's faces device endpoints, matching app/faces/signing.py."""

import hashlib
import hmac
import io
import json
import secrets
import tarfile
import time
from urllib.parse import urlsplit

import httpx

from .config import Settings

IMAGE_TRIES = 3
RETRY_PAUSE = 30


class Monica:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.http = httpx.Client(base_url=settings.api.rstrip("/"), timeout=120)

    def _headers(self, method: str, url: str, body: bytes) -> dict[str, str]:
        parts = urlsplit(str(self.http.build_request(method, url).url))
        target = parts.path + (f"?{parts.query}" if parts.query else "")
        stamp, nonce = str(int(time.time())), secrets.token_urlsafe(18)
        message = b".".join([stamp.encode(), nonce.encode(), f"{method} {target}\n".encode() + body])
        return {
            "X-Import-Timestamp": stamp,
            "X-Import-Nonce": nonce,
            "X-Import-Signature": hmac.new(self.settings.secret.encode(), message, hashlib.sha256).hexdigest(),
            "X-Import-Token": self.settings.token,
        }

    def _get(self, url: str) -> httpx.Response:
        return self.http.get(url, headers=self._headers("GET", url, b""))

    def work(self, limit: int) -> list[dict]:
        response = self._get(f"/api/v1/faces/device/work?limit={limit}")
        response.raise_for_status()
        return response.json()["items"]

    def images(self, media_ids: list[str]) -> dict[str, bytes]:
        """The original bytes by media id; an id Monica no longer has is left out.

        A busy NAS disk can stall a read past the timeout, or a commit past the statement timeout (a 5xx);
        either is retried before it is raised.
        """
        for attempt in range(1, IMAGE_TRIES + 1):
            try:
                content = self._post("/api/v1/faces/device/media", {"ids": media_ids}).content
                break
            except (httpx.ReadTimeout, httpx.HTTPStatusError) as error:
                transient = isinstance(error, httpx.ReadTimeout) or error.response.is_server_error
                if not transient or attempt == IMAGE_TRIES:
                    raise
                time.sleep(RETRY_PAUSE * attempt)
        with tarfile.open(fileobj=io.BytesIO(content)) as archive:
            return {member.name: archive.extractfile(member).read() for member in archive}

    def anchors(self) -> dict:
        response = self._get("/api/v1/faces/device/anchors")
        response.raise_for_status()
        return response.json()

    def upload(self, payload: dict) -> dict:
        return self._post("/api/v1/faces/device/batch", payload).json()

    def _post(self, url: str, payload: dict) -> httpx.Response:
        body = json.dumps(payload, separators=(",", ":")).encode()
        headers = {**self._headers("POST", url, body), "Content-Type": "application/json"}
        response = self.http.post(url, content=body, headers=headers)
        response.raise_for_status()
        return response
