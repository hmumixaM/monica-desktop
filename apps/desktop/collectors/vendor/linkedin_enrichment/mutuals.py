"""Pure parsing helpers for LinkedIn's small mutual-connection preview."""

import re
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from .browser import assert_access
from .models import MutualConnection, MutualPreview

_COUNT_PATTERN = re.compile(
    r"\b([\d,]+)\s+mutual\s+connections?\b"
    r"|([\d,]+)\s*[位个名]?\s*共同(?:联系人|好友|连接)",
    re.I,
)
_NO_MUTUALS = re.compile(
    r"\bno\s+mutual\s+connections?\b"
    r"|暂无共同(?:联系人|好友|连接)"
    r"|没有共同(?:联系人|好友|连接)",
    re.I,
)
_NON_NAME_LABELS = {
    "contact info",
    "connections",
    "message",
    "more",
    "see all mutual connections",
    "show all mutual connections",
    "查看联系信息",
    "联系人信息",
}


def _clean_url(value: str) -> str:
    absolute = urljoin("https://www.linkedin.com", value)
    parts = urlsplit(absolute)
    return urlunsplit(("https", parts.netloc.lower(), parts.path, parts.query, ""))


def _profile_url(value: str) -> str | None:
    clean = _clean_url(value)
    parts = urlsplit(clean)
    segments = [segment for segment in parts.path.split("/") if segment]
    if len(segments) < 2 or segments[0] != "in":
        return None
    return f"https://www.linkedin.com/in/{segments[1]}/"


def _is_display_name(value: str) -> bool:
    name = " ".join(value.split())
    return bool(name) and name.casefold() not in _NON_NAME_LABELS


def parse_mutual_preview(
    text: str,
    links: Iterable[Mapping[str, str] | tuple[str, str]] = (),
) -> MutualPreview:
    """Parse the profile-page mutual count, names, and optional browse URL."""

    count: int | None = 0 if _NO_MUTUALS.search(text) else None
    explicit_count = _COUNT_PATTERN.search(text)
    if explicit_count:
        matched_count = next(
            group for group in explicit_count.groups() if group is not None
        )
        count = int(matched_count.replace(",", ""))

    names: list[str] = []
    browse_url: str | None = None
    for link in links:
        if isinstance(link, Mapping):
            name = link.get("text", "").strip()
            href = link.get("href", "").strip()
        else:
            name, href = (part.strip() for part in link)
        if not href:
            continue
        clean_href = _clean_url(href)
        if "/search/results/people/" in clean_href or "facetConnectionOf" in clean_href:
            browse_url = clean_href
        elif (
            "/in/" in clean_href
            and _is_display_name(name)
            and name not in names
        ):
            names.append(name)

    preview = re.search(
        r"^\s*(.+?)\s+and\s+[\d,]+\s+other\s+mutual\s+connections?\b",
        text,
        re.I,
    )
    if preview and not names:
        for name in re.split(r"\s*,\s*|\s+and\s+", preview.group(1)):
            name = " ".join(name.split())
            if _is_display_name(name) and name not in names:
                names.append(name)

    # "You and Alice know Bob and 3 others" exposes two preview names but a
    # total of five mutuals. Explicit "N mutual connections" remains authoritative.
    others = re.search(
        r"\band\s+([\d,]+)\s+other(?:\s+mutual\s+connections?|s?)\b"
        r"|(?:其他|另外)\s*([\d,]+)\s*[位个名]?人",
        text,
        re.I,
    )
    if explicit_count is None and others:
        matched_others = next(group for group in others.groups() if group is not None)
        count = len(names) + int(matched_others.replace(",", ""))
    elif count is None and names:
        count = len(names)

    return MutualPreview(
        count=count,
        names=names,
        browse_url=browse_url,
        is_complete=count == 0,
    )


def parse_mutual_search_results(
    rows: Iterable[Mapping[str, str]],
) -> list[MutualConnection]:
    """Normalize and deduplicate visible people-search result cards."""

    connections: list[MutualConnection] = []
    seen_urls: set[str] = set()
    for row in rows:
        name = (
            row.get("display_name")
            or row.get("name")
            or row.get("text")
            or ""
        ).strip()
        href = (row.get("profile_url") or row.get("href") or "").strip()
        profile_url = _profile_url(href) if href else None
        if not profile_url or not _is_display_name(name) or profile_url in seen_urls:
            continue
        seen_urls.add(profile_url)
        connections.append(
            MutualConnection(
                display_name=" ".join(name.split()),
                profile_url=profile_url,
            )
        )
    return connections


def _search_page_url(url: str, page_number: int) -> str:
    parts = urlsplit(_clean_url(url))
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if page_number > 1:
        query["page"] = str(page_number)
    else:
        query.pop("page", None)
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), "")
    )


async def scrape_mutual_connections(
    page: Any,
    preview: MutualPreview,
    *,
    max_pages: int = 3,
    max_connections: int = 30,
) -> MutualPreview:
    """Read a small, sequential window of visible mutual-search results."""

    if (
        preview.count == 0
        or not preview.browse_url
        or max_pages < 1
        or max_connections < 1
    ):
        return preview

    connections: list[MutualConnection] = []
    seen_urls: set[str] = set()
    for page_number in range(1, max_pages + 1):
        await page.goto(
            _search_page_url(preview.browse_url, page_number),
            wait_until="domcontentloaded",
            timeout=180_000,
        )
        await assert_access(page)
        await page.wait_for_selector("main", timeout=10_000)
        await assert_access(page, expected_selector="main")
        payload = await page.evaluate(
            """() => {
              const cards = [...document.querySelectorAll(
                'main [data-view-name*="search-entity-result"], '
                + 'main .reusable-search__result-container, main li'
              )];
              const rows = [];
              for (const card of cards) {
                const link = card.querySelector('a[href*="/in/"]');
                if (!link) continue;
                const nameNode = card.querySelector(
                  '[aria-hidden="true"], .entity-result__title-text'
                );
                rows.push({
                  display_name: (
                    nameNode?.innerText || link.innerText
                    || link.getAttribute('aria-label') || ''
                  ).trim(),
                  profile_url: link.href
                });
              }
              const next = document.querySelector(
                'button[aria-label*="Next"], button[aria-label*="下一页"]'
              );
              return {
                rows,
                has_next: Boolean(
                  next && !next.disabled
                  && next.getAttribute('aria-disabled') !== 'true'
                )
              };
            }"""
        )
        for connection in parse_mutual_search_results(payload.get("rows", [])):
            if connection.profile_url in seen_urls:
                continue
            seen_urls.add(connection.profile_url)
            connections.append(connection)
            if len(connections) == max_connections:
                break

        reached_reported_count = (
            preview.count is not None and len(connections) >= preview.count
        )
        if (
            len(connections) == max_connections
            or reached_reported_count
            or not payload.get("has_next", False)
        ):
            break

    is_complete = preview.count is not None and len(connections) >= preview.count
    return preview.model_copy(
        update={"connections": connections, "is_complete": is_complete}
    )


async def scrape_mutual_preview(page: object) -> MutualPreview:
    """Extract preview text/links from the currently loaded profile page."""

    payload = await page.evaluate(
        """() => {
          const roots = [...document.querySelectorAll(
            '[data-view-name="profile-shared-connections"], '
            + '[class*="distance-badge"], [class*="mutual"], '
            + 'a[href*="facetConnectionOf"]'
          )];
          const root = roots.find(el =>
            /mutual|both know|共同/i.test(el.innerText || '')
          );
          if (!root) return {text: '', links: []};
          const container = root.closest('section, li, div') || root;
          return {
            text: container.innerText || '',
            links: [...container.querySelectorAll('a[href]')].map(a => ({
              text: (a.innerText || a.getAttribute('aria-label') || '').trim(),
              href: a.href
            }))
          };
        }"""
    )
    return parse_mutual_preview(payload.get("text", ""), payload.get("links", []))
