"""Daily discovery of the authenticated account's complete connection list."""

import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .browser import PersistentChromium, assert_access
from .models import Target
from .storage import Store, utc_now

CONNECTIONS_URL = "https://www.linkedin.com/mynetwork/invite-connect/connections/"


def canonical_connection_url(value: object) -> tuple[str, str] | None:
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    host = (parsed.hostname or "").casefold()
    segments = [segment for segment in unquote(parsed.path).split("/") if segment]
    if (
        parsed.scheme != "https"
        or host not in {"linkedin.com", "www.linkedin.com"}
        or len(segments) < 2
        or segments[0].casefold() != "in"
    ):
        return None
    slug = segments[1]
    return slug, f"https://www.linkedin.com/in/{slug}"


def connection_target(row: dict[str, Any]) -> Target | None:
    canonical = canonical_connection_url(row.get("profile_url"))
    if canonical is None:
        return None
    slug, profile_url = canonical
    metadata: dict[str, Any] = {"linkedin_connection": True}
    for source, destination in (
        ("display_name", "discovered_name"),
        ("headline", "connection_headline"),
        ("connected_on", "connected_on"),
    ):
        value = row.get(source)
        if isinstance(value, str) and value.strip():
            metadata[destination] = value.strip()
    return Target(
        profile_url=profile_url,
        external_id=slug,
        metadata=metadata,
    )


async def scrape_connections(
    user_data_dir: Path,
    *,
    headless: bool = True,
    max_scrolls: int = 60,
) -> tuple[int | None, list[Target]]:
    async with PersistentChromium(user_data_dir, headless=headless) as browser:
        page = browser.page
        await page.goto(CONNECTIONS_URL, wait_until="domcontentloaded", timeout=180_000)
        await page.wait_for_selector("main", timeout=30_000)
        await assert_access(page, expected_selector="main")

        total_match = re.search(
            r"([\d,]+)\s+connections?\b",
            await page.locator("main").inner_text(),
            re.I,
        )
        expected = (
            int(total_match.group(1).replace(",", "")) if total_match else None
        )
        found: dict[str, Target] = {}
        stable_rounds = 0
        for _ in range(max_scrolls + 1):
            rows = await page.locator("main").evaluate(
                """root => {
                  const rows = [];
                  for (const anchor of root.querySelectorAll('a[href*="/in/"]')) {
                    const name = (anchor.innerText || anchor.textContent || '').trim();
                    if (!name) continue;
                    const card = anchor.closest('li') || anchor.parentElement;
                    const lines = (card?.innerText || '').split('\\n')
                      .map(line => line.trim()).filter(Boolean);
                    const connected = lines.find(
                      line => /^Connected on /i.test(line)
                    );
                    const headline = lines.find(
                      line => line !== name
                        && !/^Connected on /i.test(line)
                        && !/^Message$/i.test(line)
                    );
                    rows.push({
                      profile_url: anchor.href,
                      display_name: name,
                      headline: headline || null,
                      connected_on: connected
                        ? connected.replace(/^Connected on /i, '')
                        : null
                    });
                  }
                  return rows;
                }"""
            )
            before = len(found)
            for row in rows:
                target = connection_target(row)
                if target is not None:
                    found[target.url] = target
            if expected is not None and len(found) >= expected:
                break

            show_more = page.get_by_role(
                "button",
                name=re.compile(r"show more results", re.I),
            )
            if await show_more.count() and await show_more.first.is_visible():
                await show_more.first.click()
            else:
                profile_links = page.locator('main a[href*="/in/"]')
                if await profile_links.count():
                    await profile_links.last.scroll_into_view_if_needed()
                await page.mouse.wheel(0, 1_200)
            await page.wait_for_timeout(2_000)
            stable_rounds = stable_rounds + 1 if len(found) == before else 0
            if stable_rounds >= 3:
                break

        return expected, list(found.values())


async def refresh_connections(
    store: Store,
    user_data_dir: Path,
    *,
    headless: bool = True,
    max_scrolls: int = 60,
) -> dict[str, int | None]:
    expected, targets = await scrape_connections(
        user_data_dir,
        headless=headless,
        max_scrolls=max_scrolls,
    )
    if expected is not None and len(targets) < expected * 0.95:
        raise RuntimeError(
            f"LinkedIn connections scan found {len(targets)} of {expected}"
        )
    store.enqueue(
        targets,
        reconcile_metadata_key=(
            "linkedin_connection"
            if expected is None or len(targets) >= expected
            else None
        ),
        now=utc_now(),
    )
    return {
        "expected": expected,
        "discovered": len(targets),
        "unavailable": max(0, (expected or len(targets)) - len(targets)),
    }
