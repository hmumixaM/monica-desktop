"""Content-addressed archive for LinkedIn-hosted images."""

import hashlib
import os
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import APIRequestContext, Page

from .storage import Store, iso, utc_now

IMAGE_EXTENSIONS = {
    "image/gif": "gif",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "video/mp4": "mp4",
    "video/quicktime": "mov",
    "video/webm": "webm",
}


def is_linkedin_asset_url(url: object) -> bool:
    if not isinstance(url, str):
        return False
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (
        host == "licdn.com" or host.endswith(".licdn.com")
    )


is_linkedin_image_url = is_linkedin_asset_url


async def page_media_urls(page: Page) -> list[str]:
    urls = await page.locator("main").evaluate(
        """root => {
          const urls = [];
          for (const image of root.querySelectorAll('img')) {
            const candidates = (image.srcset || '')
              .split(',')
              .map(candidate => candidate.trim().split(/\\s+/)[0])
              .filter(Boolean);
            urls.push(candidates.at(-1) || image.currentSrc || image.src);
          }
          for (const video of root.querySelectorAll('video')) {
            urls.push(video.poster, video.currentSrc, video.src);
            for (const source of video.querySelectorAll('source[src]')) {
              urls.push(source.src);
            }
          }
          for (const element of root.querySelectorAll('[style*="background"]')) {
            const match = getComputedStyle(element).backgroundImage
              .match(/url\\(["']?(.*?)["']?\\)/);
            if (match) urls.push(match[1]);
          }
          return [...new Set(urls.filter(Boolean))];
        }"""
    )
    return [url for url in urls if is_linkedin_asset_url(url)]


class AssetDownloadError(RuntimeError):
    """A LinkedIn image could not be archived."""


class AssetArchive:
    def __init__(
        self,
        store: Store,
        request: APIRequestContext,
        root: Path,
    ) -> None:
        self.store = store
        self.request = request
        self.root = root

    async def archive_url(self, source_url: str) -> dict[str, object]:
        if not is_linkedin_asset_url(source_url):
            raise ValueError("asset URL must use HTTPS on licdn.com")
        existing = self.store.asset_for_url(source_url)
        if existing is not None and (self.root / existing["local_path"]).is_file():
            return dict(existing)

        response = await self.request.get(source_url, timeout=120_000)
        if not response.ok:
            raise AssetDownloadError(
                f"LinkedIn asset returned HTTP {response.status}: {source_url}"
            )
        media_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        extension = IMAGE_EXTENSIONS.get(media_type)
        if extension is None:
            raise AssetDownloadError(
                f"LinkedIn asset has unsupported content type {media_type!r}"
            )
        content = await response.body()
        digest = hashlib.sha256(content).hexdigest()
        local_path = f"{digest}.{extension}"
        destination = self.root / local_path
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o755)
        if not destination.exists():
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{digest}.",
                suffix=".tmp",
                dir=self.root,
            )
            try:
                with os.fdopen(descriptor, "wb") as temporary:
                    temporary.write(content)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                os.chmod(temporary_name, 0o644)
                os.replace(temporary_name, destination)
            finally:
                if os.path.exists(temporary_name):
                    os.unlink(temporary_name)

        captured_at = iso(utc_now())
        self.store.record_asset(
            source_url=source_url,
            content_hash=digest,
            media_type=media_type,
            byte_count=len(content),
            local_path=local_path,
            captured_at=captured_at,
        )
        archived = self.store.asset_for_url(source_url)
        if archived is None:
            raise RuntimeError("archived asset was not persisted")
        return dict(archived)

    async def archive_urls(self, source_urls: list[str]) -> tuple[int, int]:
        archived = 0
        failed = 0
        for source_url in dict.fromkeys(source_urls):
            try:
                await self.archive_url(source_url)
            except AssetDownloadError:
                failed += 1
            else:
                archived += 1
        return archived, failed
