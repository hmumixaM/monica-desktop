"""Small adaptations of the pinned upstream async scrapers."""

import re
from collections.abc import Collection
from urllib.parse import urlsplit

from linkedin_scraper import CompanyPostsScraper, PersonScraper

from .browser import AccessBlocked, AccessSignal, assert_access
from .models import ExtendedPerson, ExtendedPost
from .mutuals import scrape_mutual_connections, scrape_mutual_preview
from .profile_media import profile_dom_media
from .voyager import scrape_voyager_profile


def personal_posts_url(profile_url: str) -> str:
    """Return LinkedIn's canonical personal recent-activity URL."""

    parts = urlsplit(profile_url)
    if parts.scheme != "https" or parts.hostname not in {
        "linkedin.com",
        "www.linkedin.com",
    }:
        raise ValueError("profile_url must be an HTTPS LinkedIn URL")
    segments = [segment for segment in parts.path.split("/") if segment]
    if len(segments) < 2 or segments[0] != "in":
        raise ValueError("profile_url must contain /in/<slug>/")
    return f"https://www.linkedin.com/in/{segments[1]}/recent-activity/all/"


def _profile_identity(profile_url: str | None) -> str | None:
    if not profile_url:
        return None
    parts = urlsplit(profile_url)
    segments = [segment for segment in parts.path.split("/") if segment]
    if len(segments) < 2 or segments[0].lower() != "in":
        return None
    return segments[1].casefold()


def classify_post_authorship(
    profile_url: str,
    *,
    actor_profile_url: str | None,
    repost_header: str | None,
) -> tuple[bool | None, str]:
    """Classify only strong DOM evidence; uncertainty remains collectable."""

    if repost_header and re.search(
        r"\b(?:reposted|shared)\s+this\b|转发了|分享了",
        repost_header,
        re.I,
    ):
        return False, "repost"
    target = _profile_identity(profile_url)
    actor = _profile_identity(actor_profile_url)
    if target and actor_profile_url and urlsplit(actor_profile_url).path.startswith("/company/"):
        return False, "other_actor"
    if target and actor:
        return (True, "post") if target == actor else (False, "other_actor")
    return None, "unknown"


class ManualCheckpointMixin:
    manual_checkpoint_timeout_minutes: int

    async def check_rate_limit(self) -> None:
        try:
            await assert_access(self.page)
        except AccessBlocked as error:
            if (
                error.signal is not AccessSignal.CHECKPOINT
                or self.manual_checkpoint_timeout_minutes < 1
            ):
                raise
            for _ in range(self.manual_checkpoint_timeout_minutes * 6):
                await self.page.wait_for_timeout(10_000)
                try:
                    await assert_access(self.page)
                except AccessBlocked as retry_error:
                    if retry_error.signal is AccessSignal.CHECKPOINT:
                        continue
                    raise
                return
            raise


class PersonalPostsScraper(ManualCheckpointMixin, CompanyPostsScraper):
    """Reuse upstream company-post parsing on a person's activity stream."""

    def __init__(
        self,
        page: object,
        *,
        manual_checkpoint_timeout_minutes: int = 0,
    ):
        super().__init__(page)
        self.manual_checkpoint_timeout_minutes = manual_checkpoint_timeout_minutes
        self._profile_url: str | None = None

    async def _extract_posts_from_page(self) -> list[ExtendedPost]:
        posts = await super()._extract_posts_from_page()
        if not posts:
            from .activity_posts import extract_activity_cards
            if not hasattr(self, "_activity_identities"):
                self._activity_identities = {}
            rows = await extract_activity_cards(self.page, identities=self._activity_identities)
            result = []
            for row in rows:
                authored, activity = classify_post_authorship(
                    self._profile_url or "", actor_profile_url=row.get("actor_profile_url"),
                    repost_header=row.get("repost_header"))
                result.append(ExtendedPost(**row, is_authored=authored, activity_type=activity))
            return result
        metadata_rows = await self.page.evaluate(
            """() => {
              const rows = [];
              const seen = new Set();
              for (const element of document.querySelectorAll(
                '[data-urn^="urn:li:activity:"]'
              )) {
                const urn = element.getAttribute('data-urn');
                if (!urn || seen.has(urn)) continue;
                seen.add(urn);
                const actor = element.querySelector(
                  '.update-components-actor__meta a[href*="/in/"], '
                  + '.feed-shared-actor__container-link[href*="/in/"], '
                  + '.update-components-actor__container-link[href*="/in/"]'
                );
                const header = element.querySelector(
                  '.update-components-header__text-view, '
                  + '.feed-shared-header__text-view, '
                  + '.update-components-header__text-wrapper'
                );
                const headerText = (header?.innerText || '').trim();
                const imageUrls = [];
                for (const image of element.querySelectorAll('img')) {
                  const candidates = (image.srcset || '')
                    .split(',')
                    .map(candidate => candidate.trim().split(/\\s+/)[0])
                    .filter(Boolean);
                  const url = candidates.at(-1) || image.currentSrc || image.src;
                  if (
                    url
                    && /https:\\/\\/[^/]*licdn\\.com\\//i.test(url)
                    && !/profile-displayphoto|company-logo|school-logo/i.test(url)
                  ) {
                    imageUrls.push(url);
                  }
                }
                const videoUrls = [];
                for (const video of element.querySelectorAll('video')) {
                  if (video.poster) imageUrls.push(video.poster);
                  for (const url of [
                    video.currentSrc,
                    video.src,
                    ...[...video.querySelectorAll('source[src]')]
                      .map(source => source.src)
                  ]) {
                    if (url && /https:\\/\\/[^/]*licdn\\.com\\//i.test(url)) {
                      videoUrls.push(url);
                    }
                  }
                }
                rows.push({
                  urn,
                  actor_profile_url: actor?.href || null,
                  repost_header: /\\b(reposted|shared)\\s+this\\b|转发了|分享了/i
                    .test(headerText) ? headerText : null,
                  image_urls: [...new Set(imageUrls)],
                  video_urls: [...new Set(videoUrls)]
                });
              }
              return rows;
            }"""
        )
        metadata = {row["urn"]: row for row in metadata_rows}
        result = []
        for post in posts:
            row = metadata.get(post.urn, {})
            is_authored, activity_type = classify_post_authorship(
                self._profile_url or "",
                actor_profile_url=row.get("actor_profile_url"),
                repost_header=row.get("repost_header"),
            )
            post_data = post.model_dump()
            post_data["image_urls"] = list(
                dict.fromkeys(
                    [
                        *row.get("image_urls", []),
                        *post_data.get("image_urls", []),
                    ]
                )
            )
            if row.get("video_urls"):
                post_data["video_url"] = row["video_urls"][0]
            result.append(
                ExtendedPost(
                    **post_data,
                    actor_profile_url=row.get("actor_profile_url"),
                    repost_header=row.get("repost_header"),
                    is_authored=is_authored,
                    activity_type=activity_type,
                )
            )
        return result

    async def scrape(
        self,
        profile_url: str,
        limit: int = 10,
        *,
        max_scrolls: int = 5,
        known_urns: Collection[str] = (),
    ) -> list[ExtendedPost]:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if max_scrolls < 0:
            raise ValueError("max_scrolls cannot be negative")

        self._profile_url = profile_url
        url = personal_posts_url(profile_url)
        await self.callback.on_start("personal_posts", url)
        await self.navigate_and_wait(url)
        await self.check_rate_limit()
        await self._wait_for_posts_to_load()

        posts: list[ExtendedPost] = []
        seen: set[str] = set()
        known = set(known_urns)
        scrolls = 0
        stop = False
        while len(posts) < limit:
            for post in await self._extract_posts_from_page():
                if not post.urn or post.urn in seen:
                    continue
                seen.add(post.urn)
                if post.urn in known:
                    # Pinned posts and ranked activity are not chronological.
                    # Keep scanning the bounded window for unseen posts.
                    continue
                posts.append(post)
                if len(posts) == limit:
                    break
            if stop or len(posts) == limit or scrolls == max_scrolls:
                break
            await self._scroll_for_more_posts()
            scrolls += 1

        await self.callback.on_complete("personal_posts", posts)
        return posts


class SafePersonScraper(ManualCheckpointMixin, PersonScraper):
    """Person scraper with checkpoint/rate checks limited to visible content."""

    def __init__(
        self,
        page: object,
        *,
        manual_checkpoint_timeout_minutes: int = 0,
    ):
        super().__init__(page)
        self.manual_checkpoint_timeout_minutes = manual_checkpoint_timeout_minutes


async def scrape_extended_person(
    page: object,
    profile_url: str,
    *,
    manual_checkpoint_timeout_minutes: int = 0,
) -> ExtendedPerson:
    """Collect structured profile data and a bounded mutual-connection window."""

    scraper = SafePersonScraper(
        page,
        manual_checkpoint_timeout_minutes=manual_checkpoint_timeout_minutes,
    )
    await scraper.callback.on_start("person", profile_url)
    await scraper.navigate_and_wait(profile_url)
    await scraper.ensure_logged_in()
    await page.wait_for_selector("main", timeout=10_000)
    await scraper.wait_and_focus(1)
    person = await scrape_voyager_profile(page, profile_url)
    avatar_url, background_url = await profile_dom_media(page, profile_url)
    person = person.model_copy(
        update={
            "avatar_url": avatar_url or person.avatar_url,
            "background_url": background_url or person.background_url,
        }
    )
    mutual = await scrape_mutual_preview(page)
    mutual = await scrape_mutual_connections(page, mutual)
    person = person.model_copy(
        update={
            "mutual_count": mutual.count,
            "mutual_preview_names": mutual.names,
            "mutual_browse_url": mutual.browse_url,
            "mutual_connections": mutual.connections,
            "mutual_is_complete": mutual.is_complete,
        }
    )
    await scraper.callback.on_complete("person", person)
    return person
