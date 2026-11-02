"""One-target collector orchestration."""

from datetime import timedelta
from pathlib import Path
from typing import Any

from linkedin_scraper.core.exceptions import (
    AuthenticationError,
    LinkedInScraperException,
    NetworkError,
    RateLimitError,
)

from .assets import AssetArchive, is_linkedin_asset_url, page_media_urls
from .browser import AccessBlocked, AccessSignal, PersistentChromium, assert_access
from .models import dump_post
from .scrapers import PersonalPostsScraper, scrape_extended_person
from .storage import Store, iso, utc_now


async def collect_claimed_job(
    store: Store,
    job: Any,
    *,
    user_data_dir: Path,
    headless: bool,
    post_limit: int,
    max_scrolls: int,
    asset_dir: Path = Path("data/linkedin-assets"),
    manual_checkpoint_timeout_minutes: int = 0,
    browser: PersistentChromium | None = None,
) -> None:
    """Collect profile, post window, and mutual preview for one leased job."""

    statuses: list[int] = []
    try:
        if browser is None:
            async with PersistentChromium(
                user_data_dir,
                headless=headless,
            ) as owned_browser:
                posts = await _collect_in_browser(
                    store,
                    job,
                    owned_browser,
                    statuses=statuses,
                    post_limit=post_limit,
                    max_scrolls=max_scrolls,
                    asset_dir=asset_dir,
                    manual_checkpoint_timeout_minutes=(
                        manual_checkpoint_timeout_minutes
                    ),
                )
        else:
            posts = await _collect_in_browser(
                store,
                job,
                browser,
                statuses=statuses,
                post_limit=post_limit,
                max_scrolls=max_scrolls,
                asset_dir=asset_dir,
                manual_checkpoint_timeout_minutes=(
                    manual_checkpoint_timeout_minutes
                ),
            )

        store.archive_snapshot(
            job["target_id"],
            "posts",
            [dump_post(post) for post in posts],
            complete=False,
        )
        store.complete_job(job["job_id"])
    except AccessBlocked as error:
        paused_until = store.trip_circuit(error.signal)
        store.fail_job(job["job_id"], str(error), retry_at=paused_until)
        raise
    except LinkedInScraperException as error:
        if isinstance(error, NetworkError):
            store.fail_job(
                job["job_id"],
                str(error),
                retry_at=utc_now() + timedelta(hours=24),
            )
            raise
        message = str(error).lower()
        signal = (
            AccessSignal.AUTH
            if isinstance(error, AuthenticationError)
            else AccessSignal.RATE_LIMIT
            if isinstance(error, RateLimitError)
            else AccessSignal.CHECKPOINT
            if "checkpoint" in message or "captcha" in message
            else AccessSignal.SELECTOR_DRIFT
        )
        paused_until = store.trip_circuit(signal)
        store.fail_job(job["job_id"], str(error), retry_at=paused_until)
        raise
    except Exception as error:
        store.fail_job(
            job["job_id"],
            str(error),
            retry_at=utc_now() + timedelta(hours=24),
        )
        raise


async def _collect_in_browser(
    store: Store,
    job: Any,
    browser: PersistentChromium,
    *,
    statuses: list[int],
    post_limit: int,
    max_scrolls: int,
    asset_dir: Path = Path("data/linkedin-assets"),
    manual_checkpoint_timeout_minutes: int = 0,
) -> list[Any]:
    page = browser.page
    asset_archive = AssetArchive(store, browser.context.request, asset_dir)

    def remember_status(response: Any) -> None:
        if "linkedin.com" in response.url and response.status in {401, 403, 429}:
            statuses.append(response.status)

    page.on("response", remember_status)
    profile_due_at = job["profile_due_at"]
    if not profile_due_at or profile_due_at <= iso(utc_now()):
        person = await scrape_extended_person(
            page,
            job["profile_url"],
            manual_checkpoint_timeout_minutes=manual_checkpoint_timeout_minutes,
        )
        await assert_access(
            page,
            response_status=statuses[-1] if statuses else None,
            expected_selector="main",
        )
        profile = person.model_dump(
            mode="json",
            exclude={
                "mutual_count",
                "mutual_preview_names",
                "mutual_browse_url",
                "mutual_connections",
                "mutual_is_complete",
            },
        )
        profile_asset_urls = [
            url
            for url in (profile.get("avatar_url"), profile.get("background_url"))
            if is_linkedin_asset_url(url)
        ]
        profile_asset_urls.extend(await page_media_urls(page))
        await asset_archive.archive_urls(profile_asset_urls)
        mutuals = {
            "count": person.mutual_count,
            "preview_names": person.mutual_preview_names,
            "connections": [
                connection.model_dump(mode="json")
                for connection in person.mutual_connections
            ],
            "browse_url": person.mutual_browse_url,
            "is_complete": person.mutual_is_complete,
        }
        profile_complete = bool(
            person.name and person.name.strip().lower() != "unknown"
        )
        store.archive_snapshot(
            job["target_id"],
            "profile",
            profile,
            complete=profile_complete,
        )
        store.archive_snapshot(
            job["target_id"],
            "mutuals",
            mutuals,
            complete=person.mutual_is_complete,
        )
        if not profile_complete:
            raise AccessBlocked(
                AccessSignal.SELECTOR_DRIFT,
                "profile name selector produced only the Unknown sentinel",
            )
        store.mark_profile_collected(job["target_id"])

    posts = await PersonalPostsScraper(
        page,
        manual_checkpoint_timeout_minutes=manual_checkpoint_timeout_minutes,
    ).scrape(
        job["profile_url"],
        post_limit,
        max_scrolls=max_scrolls,
        known_urns=store.known_post_urns(job["target_id"]),
    )
    image_urls = [
        media_url
        for post in posts
        for media_url in [*post.image_urls, post.video_url]
        if is_linkedin_asset_url(media_url)
    ]
    image_urls.extend(await page_media_urls(page))
    await asset_archive.archive_urls(image_urls)
    await assert_access(
        page,
        response_status=statuses[-1] if statuses else None,
        expected_selector="main",
    )
    page.remove_listener("response", remember_status)
    return posts


async def run_once(
    store: Store,
    *,
    user_data_dir: Path,
    window_cap: int = 3,
    window_hours: int = 2,
    rolling_cap: int = 36,
    rolling_hours: int = 24,
    profile_url: str | None = None,
    headless: bool = True,
    post_limit: int = 10,
    max_scrolls: int = 5,
    asset_dir: Path = Path("data/linkedin-assets"),
    manual_checkpoint_timeout_minutes: int = 0,
) -> bool:
    """Lease and process no more than one contact."""

    job = store.claim(
        window_cap=window_cap,
        window_hours=window_hours,
        rolling_cap=rolling_cap,
        rolling_hours=rolling_hours,
        profile_url=profile_url,
    )
    if job is None:
        return False
    await collect_claimed_job(
        store,
        job,
        user_data_dir=user_data_dir,
        headless=headless,
        post_limit=post_limit,
        max_scrolls=max_scrolls,
        asset_dir=asset_dir,
        manual_checkpoint_timeout_minutes=manual_checkpoint_timeout_minutes,
    )
    return True


async def run_batch(
    store: Store,
    *,
    user_data_dir: Path,
    max_contacts: int = 3,
    window_cap: int = 3,
    window_hours: int = 2,
    rolling_cap: int = 36,
    rolling_hours: int = 24,
    profile_urls: list[str] | None = None,
    headless: bool = True,
    post_limit: int = 10,
    max_scrolls: int = 5,
    asset_dir: Path = Path("data/linkedin-assets"),
    manual_checkpoint_timeout_minutes: int = 0,
) -> int:
    """Process several contacts in one persistent browser session."""

    processed = 0
    batch_size = (
        min(max_contacts, len(profile_urls))
        if profile_urls is not None
        else max_contacts
    )
    async with PersistentChromium(user_data_dir, headless=headless) as browser:
        for index in range(batch_size):
            job = store.claim(
                window_cap=window_cap,
                window_hours=window_hours,
                rolling_cap=rolling_cap,
                rolling_hours=rolling_hours,
                profile_url=(
                    profile_urls[index]
                    if profile_urls is not None and index < len(profile_urls)
                    else None
                ),
            )
            if job is None:
                break
            try:
                await collect_claimed_job(
                    store,
                    job,
                    user_data_dir=user_data_dir,
                    headless=headless,
                    post_limit=post_limit,
                    max_scrolls=max_scrolls,
                    asset_dir=asset_dir,
                    manual_checkpoint_timeout_minutes=(
                        manual_checkpoint_timeout_minutes
                    ),
                    browser=browser,
                )
            except NetworkError:
                continue
            except (AccessBlocked, LinkedInScraperException):
                raise
            except Exception:
                continue
            else:
                processed += 1
    return processed
