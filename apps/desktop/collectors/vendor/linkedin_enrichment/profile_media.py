"""Largest profile media variants from an existing Voyager response."""

from collections.abc import Iterator, Mapping
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .assets import is_linkedin_asset_url


def _vectors(value: Any) -> Iterator[tuple[int, str]]:
    if isinstance(value, Mapping):
        root = value.get("rootUrl")
        artifacts = value.get("artifacts")
        if isinstance(root, str) and isinstance(artifacts, list):
            for artifact in artifacts:
                if not isinstance(artifact, Mapping):
                    continue
                suffix = artifact.get("fileIdentifyingUrlPathSegment")
                if isinstance(suffix, str):
                    size = int(artifact.get("width") or 0) * int(
                        artifact.get("height") or 0
                    )
                    yield size, root + suffix
        for child in value.values():
            yield from _vectors(child)
    elif isinstance(value, list):
        for child in value:
            yield from _vectors(child)


def _largest(entity: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    candidates = [
        candidate
        for key in keys
        for candidate in _vectors(entity.get(key))
        if is_linkedin_asset_url(candidate[1])
    ]
    return max(candidates, default=(0, None))[1]


def profile_media(
    raw: Mapping[str, Any],
    public_identifier: str,
) -> tuple[str | None, str | None]:
    for entity in raw.get("included", []):
        if not isinstance(entity, Mapping):
            continue
        if (
            str(entity.get("publicIdentifier", "")).casefold()
            != public_identifier.casefold()
        ):
            continue
        avatar = _largest(
            entity,
            ("profilePicture", "picture", "displayImageReference"),
        )
        background = _largest(
            entity,
            ("backgroundPicture", "backgroundImage", "profileBackgroundPicture"),
        )
        return avatar, background
    return None, None


def _linked_profile(href: str | None) -> str | None:
    if not href:
        return None
    segments = [segment for segment in urlsplit(href).path.split("/") if segment]
    if len(segments) < 2 or segments[0].casefold() != "in":
        return None
    return segments[1].casefold()


def _belongs_to_profile(href: str | None, profile_url: str) -> bool:
    if not href:
        return True
    path = urlsplit(href).path.casefold()
    if path.startswith("/search/results/people"):
        return False
    linked_profile = _linked_profile(href)
    return linked_profile is None or linked_profile == _linked_profile(profile_url)


async def profile_dom_media(
    page: Page,
    profile_url: str,
) -> tuple[str | None, str | None]:
    try:
        await page.wait_for_selector(
            'main img[src*="profile-displayphoto"], '
            'main img[srcset*="profile-displayphoto"], '
            'main img[src*="profile-framedphoto"], '
            'main img[srcset*="profile-framedphoto"]',
            timeout=10_000,
        )
    except PlaywrightTimeoutError:
        return None, None
    rows = await page.locator("main img").evaluate_all(
        """images => images.map(image => {
          const candidates = (image.srcset || '')
            .split(',')
            .map(candidate => {
              const parts = candidate.trim().split(/\\s+/);
              return {
                url: parts[0],
                width: Number.parseInt(parts[1] || '0', 10) || 0
              };
            })
            .filter(candidate => candidate.url);
          candidates.push({
            url: image.currentSrc || image.src,
            width: image.naturalWidth || 0
          });
          const link = image.closest('a[href]');
          return candidates.map(candidate => ({
            ...candidate,
            href: link?.href || null
          }));
        }).flat()"""
    )
    avatar_candidates = [
        (int(row.get("width") or 0), row.get("url"))
        for row in rows
        if isinstance(row, dict)
        and isinstance(row.get("url"), str)
        and (
            "profile-displayphoto" in row["url"]
            or "profile-framedphoto" in row["url"]
        )
        and is_linkedin_asset_url(row["url"])
        and _belongs_to_profile(row.get("href"), profile_url)
    ]
    background_candidates = [
        (int(row.get("width") or 0), row.get("url"))
        for row in rows
        if isinstance(row, dict)
        and isinstance(row.get("url"), str)
        and "profile-displaybackgroundimage" in row["url"]
        and is_linkedin_asset_url(row["url"])
        and _belongs_to_profile(row.get("href"), profile_url)
    ]
    return (
        max(avatar_candidates, default=(0, None))[1],
        max(background_candidates, default=(0, None))[1],
    )
