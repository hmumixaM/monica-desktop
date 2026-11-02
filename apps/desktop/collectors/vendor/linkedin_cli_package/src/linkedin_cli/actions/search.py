import json
import logging
import re
from typing import Dict, Any
from urllib.parse import urlparse, parse_qs, urlencode, urljoin

from linkedin_cli.browser.nav import goto_page, extract_in_urls

# LinkedIn connection-degree filter codes for People search (`network` facet).
NETWORK_CODES = {"first": "F", "second": "S", "third": "O"}

logger = logging.getLogger(__name__)

SELECTORS = {
    "search_bar": "//input[contains(@placeholder, 'Search')]",
    "profile_links": 'a[href*="/in/"]',
}

ACTION_LABELS = {"Message", "Connect", "Follow", "Following", "Pending", "More"}


def _go_to_profile(session: "LinkedInSession", url: str, public_identifier: str):
    if f"/in/{public_identifier}" in session.page.url:
        return
    logger.debug("Direct navigation → %s", public_identifier)
    try:
        goto_page(
            session,
            action=lambda: session.page.goto(url, wait_until="domcontentloaded"),
            expected_url_pattern=f"/in/{public_identifier}",
            error_message="Failed to navigate to the target profile"
        )
    except RuntimeError:
        # Redirect to a different /in/ slug is tolerated; reconciling the
        # lead's stored slug is the caller's job (this layer holds no DB).
        if not _detect_profile_redirect(session, public_identifier):
            raise


def _detect_profile_redirect(session, old_public_id: str) -> str | None:
    """Return the new public_id if LinkedIn redirected to a different /in/ slug."""
    from urllib.parse import unquote
    from linkedin_cli.url_utils import url_to_public_id

    new_id = url_to_public_id(unquote(session.page.url))
    if new_id and new_id != old_public_id:
        logger.info("Profile redirect: %s → %s", old_public_id, new_id)
        return new_id
    return None


def visit_profile(session: "LinkedInSession", profile: Dict[str, Any]):
    public_identifier = profile.get("public_identifier")

    # Ensure browser is alive before doing anything
    session.ensure_browser()

    already_there = f"/in/{public_identifier}" in session.page.url

    if already_there:
        return

    url = profile.get("url")
    _go_to_profile(session, url, public_identifier)

    # Emit the /in/ profile URLs visible on the page; enrichment is caller-side.
    return extract_in_urls(session.page)


def _json_facet(values) -> str:
    return json.dumps(list(values), separators=(",", ":"))


def _search_url(
    keyword: str,
    page: int = 1,
    network=None,
    *,
    geo_urn: list[str] | None = None,
    current_company: list[str] | None = None,
    is_verified: bool = False,
    connection_of: list[str] | None = None,
    follower_of: list[str] | None = None,
    past_company: list[str] | None = None,
    school: list[str] | None = None,
    industry: list[str] | None = None,
    profile_language: list[str] | None = None,
    open_to_volunteer: bool = False,
    service_category: list[str] | None = None,
) -> str:
    """Build a People-search results URL, optionally filtered by connection degree.

    *network* is an optional list of degree codes — ``F`` (1st), ``S`` (2nd),
    ``O`` (3rd+) — passed to LinkedIn's ``network`` facet as a JSON array.
    """
    params = {"keywords": keyword, "origin": "FACETED_SEARCH"}
    if network:
        params["network"] = _json_facet(network)
    if geo_urn:
        params["geoUrn"] = _json_facet(geo_urn)
    if current_company:
        params["currentCompany"] = _json_facet(current_company)
    if is_verified:
        params["isVerified"] = _json_facet(["true"])
    if connection_of:
        params["connectionOf"] = _json_facet(connection_of)
    if follower_of:
        params["followerOf"] = _json_facet(follower_of)
    if past_company:
        params["pastCompany"] = _json_facet(past_company)
    if school:
        params["schoolFilter"] = _json_facet(school)
    if industry:
        params["industry"] = _json_facet(industry)
    if profile_language:
        params["profileLanguage"] = _json_facet(profile_language)
    if open_to_volunteer:
        params["openToVolunteer"] = _json_facet(["true"])
    if service_category:
        params["serviceCategory"] = _json_facet(service_category)
    if page > 1:
        params["page"] = page
    return "https://www.linkedin.com/search/results/people/?" + urlencode(params)


def _clean_profile_url(base_url: str, href: str) -> str:
    full_url = urljoin(base_url, href.strip())
    parsed = urlparse(full_url)
    return parsed._replace(query="", fragment="").geturl()


def _text_lines(text: str) -> list[str]:
    seen = set()
    lines = []
    for raw in text.splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if not line or line in seen:
            continue
        seen.add(line)
        lines.append(line)
    return lines


def _compact_count(value: str) -> int | None:
    match = re.search(r"(\d[\d,.]*)\s*([KMB])?", value, flags=re.IGNORECASE)
    if not match:
        return None
    number = float(match.group(1).replace(",", ""))
    suffix = (match.group(2) or "").upper()
    multiplier = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(suffix, 1)
    return int(number * multiplier)


def _first_inner_text(locator) -> str:
    try:
        if locator.count() > 0:
            return locator.first.inner_text().strip()
    except Exception:
        return ""
    return ""


def _nearest_search_card(link, full_name: str | None):
    return link.evaluate_handle(
        r"""
        (el, name) => {
          let node = el;
          let candidate = null;
          for (let depth = 0; depth < 12 && node && node.parentElement; depth += 1) {
            node = node.parentElement;
            const text = (node.innerText || '').replace(/\s+/g, ' ').trim();
            if (!text.includes(name)) continue;
            const hasNamedAction = Array.from(node.querySelectorAll('button[aria-label]'))
              .some((button) => (button.getAttribute('aria-label') || '').includes(name));
            const hasResultDetail = /^.+\b(1st|2nd|3rd\+?)\b/.test(text) && text.length > name.length + 20;
            const degreeCount = (text.match(/\b(1st|2nd|3rd\+?)\b/g) || []).length;
            if ((hasNamedAction || hasResultDetail) && degreeCount <= 1) {
              candidate = node;
            }
          }
          return candidate || el.parentElement || el;
        }
        """,
        full_name or "",
    ).as_element()


def _button_available(card, pattern: str) -> bool:
    try:
        return card.evaluate(
            """
            (el, pattern) => Array.from(el.querySelectorAll('button[aria-label]'))
              .some((button) => (button.getAttribute('aria-label') || '')
                .toLowerCase().includes(pattern.toLowerCase()))
            """,
            pattern,
        )
    except Exception:
        return False


def _people_result_from_link(page, link) -> dict | None:
    from linkedin_cli.url_utils import url_to_public_id

    href = link.get_attribute("href") or ""
    url = _clean_profile_url(page.url, href)
    public_id = url_to_public_id(url)
    if not public_id:
        return None

    full_name = link.evaluate(
        r"""
        (el) => {
          let text = Array.from(el.childNodes)
            .filter((node) => node.nodeType === Node.TEXT_NODE)
            .map((node) => node.textContent || '')
            .join(' ')
            .replace(/\s+/g, ' ')
            .trim();
          if (!text) text = (el.innerText || '').replace(/\s+/g, ' ').trim();
          return text.split(/\s+•\s+/)[0].trim();
        }
        """
    ) or None
    card = _nearest_search_card(link, full_name)
    if card is None:
        return {"public_identifier": public_id, "url": url, "full_name": full_name}

    link_context = _first_inner_text(link.locator("xpath=ancestor::p[1]"))
    card_text = card.inner_text().strip()
    context_degree_match = re.search(r"\b(1st|2nd|3rd\+?)\b", link_context)

    if not context_degree_match:
        # Skip incidental profile links inside a result card, such as mutual connections.
        return None

    connection_degree = context_degree_match.group(1)

    lines = _text_lines(card_text)
    detail_lines = []
    followers_text = None
    mutual_connections_text = None
    for line in lines:
        line_without_degree = re.sub(r"(?:^|\s*•\s*)\b(?:1st|2nd|3rd\+?)\b", "", line).strip()
        if not line_without_degree:
            continue
        if full_name and line_without_degree == full_name:
            continue
        if line in ACTION_LABELS:
            continue
        if "followers" in line.lower():
            followers_text = line
            continue
        if "mutual connection" in line.lower():
            mutual_connections_text = line
            continue
        detail_lines.append(line_without_degree)

    image_url = None
    image = card.query_selector('img[src*="profile-displayphoto"]')
    if image is not None:
        image_url = image.get_attribute("src")

    headline = detail_lines[0] if detail_lines else None
    location = detail_lines[1] if len(detail_lines) > 1 else None
    result = {
        "public_identifier": public_id,
        "url": url,
        "full_name": full_name,
        "headline": headline,
        "location": location,
        "connection_degree": connection_degree,
        "profile_image_url": image_url,
        "verified": card.query_selector('[aria-label="Verified"]') is not None,
        "can_message": _button_available(card, "message"),
        "can_connect": _button_available(card, "connect"),
        "can_follow": _button_available(card, "follow"),
        "followers": _compact_count(followers_text) if followers_text else None,
        "followers_text": followers_text,
        "mutual_connections": _compact_count(mutual_connections_text) if mutual_connections_text else None,
        "mutual_connections_text": mutual_connections_text,
    }
    return result


def _initiate_search(session: "LinkedInSession", keyword: str):
    """Navigate directly to LinkedIn People search results for *keyword*."""
    goto_page(
        session,
        action=lambda: session.page.goto(_search_url(keyword)),
        expected_url_pattern="/search/results/people/",
        error_message="Failed to reach People search results",
    )


def _paginate_to_next_page(session: "LinkedInSession", page_num: int):
    page = session.page
    current = urlparse(page.url)
    params = parse_qs(current.query)
    params["page"] = [str(page_num)]
    new_url = current._replace(query=urlencode(params, doseq=True)).geturl()

    logger.debug("Scanning search page %s", page_num)
    goto_page(
        session,
        action=lambda: page.goto(new_url),
        expected_url_pattern="/search/results/",
        error_message="Pagination failed"
    )


def search_people(
    session: "LinkedInSession",
    keyword: str,
    page: int = 1,
    network=None,
    *,
    limit: int = 10,
    geo_urn: list[str] | None = None,
    current_company: list[str] | None = None,
    is_verified: bool = False,
    connection_of: list[str] | None = None,
    follower_of: list[str] | None = None,
    past_company: list[str] | None = None,
    school: list[str] | None = None,
    industry: list[str] | None = None,
    profile_language: list[str] | None = None,
    open_to_volunteer: bool = False,
    service_category: list[str] | None = None,
) -> dict:
    """Search LinkedIn People; return the result page as a structured envelope.

    *network* optionally filters by connection degree (a list of `F`/`S`/`O`
    codes). Results carry visible search-card fields but no `urn`; a follow-up
    `profile` scrape per url resolves the rest. Returns::

        {"query": ..., "page": ..., "network": [...]|None,
         "profiles": [{"public_identifier": ..., "url": ..., ...}, ...]}
    """
    from linkedin_cli.url_utils import url_to_public_id

    session.ensure_browser()
    goto_page(
        session,
        action=lambda: session.page.goto(_search_url(
            keyword,
            page,
            network,
            geo_urn=geo_urn,
            current_company=current_company,
            is_verified=is_verified,
            connection_of=connection_of,
            follower_of=follower_of,
            past_company=past_company,
            school=school,
            industry=industry,
            profile_language=profile_language,
            open_to_volunteer=open_to_volunteer,
            service_category=service_category,
        )),
        expected_url_pattern="/search/results/people/",
        error_message="Failed to reach People search results",
    )

    profiles, seen = [], set()
    for link in session.page.locator(SELECTORS["profile_links"]).all():
        profile = _people_result_from_link(session.page, link)
        if not profile:
            continue
        public_id = profile.get("public_identifier")
        if public_id and public_id not in seen:
            seen.add(public_id)
            profiles.append(profile)
            if len(profiles) >= limit:
                break

    if not profiles:
        for url in extract_in_urls(session.page):
            public_id = url_to_public_id(url)
            if public_id and public_id not in seen:
                seen.add(public_id)
                profiles.append({"public_identifier": public_id, "url": url})
                if len(profiles) >= limit:
                    break

    filters = {
        "network": list(network) if network else [],
        "geo_urn": geo_urn or [],
        "current_company": current_company or [],
        "is_verified": is_verified,
        "connection_of": connection_of or [],
        "follower_of": follower_of or [],
        "past_company": past_company or [],
        "school": school or [],
        "industry": industry or [],
        "profile_language": profile_language or [],
        "open_to_volunteer": open_to_volunteer,
        "service_category": service_category or [],
    }
    return {
        "query": keyword,
        "page": page,
        "network": list(network) if network else None,
        "filters": filters,
        "profiles": profiles,
    }


def _simulate_human_search(session: "LinkedInSession", profile: Dict[str, Any]) -> bool:
    full_name = profile.get("full_name")
    public_identifier = profile.get("public_identifier")

    # Reconstruct full_name if it's missing
    if not full_name:
        first = profile.get("first_name", "").strip()
        last = profile.get("last_name", "").strip()
        if first or last:
            full_name = f"{first} {last}".strip() if first and last else (first or last)
        else:
            logger.error(f"No name available for {public_identifier}")
            logger.debug(profile)
            return False

    if not public_identifier:
        logger.error(f"Missing public_identifier for '{full_name}'")
        raise ValueError("public_identifier is required")

    logger.info(f"Human search → '{full_name}' (target: {public_identifier})")

    _initiate_search(session, full_name)

    max_pages_to_scan = 1

    for current_page in range(1, max_pages_to_scan + 1):
        logger.info("Scanning search results page %s", current_page)

        target_locator = None
        for link in session.page.locator(SELECTORS["profile_links"]).all():
            href = link.get_attribute("href") or ""
            if f"/in/{public_identifier}" in href:
                target_locator = link
                break

        if target_locator:
            logger.info("Target found in results → clicking")
            return False

        if session.page.get_by_text("No results found", exact=False).count() > 0:
            logger.info("No results found → stopping search")
            break

        if current_page < max_pages_to_scan:
            _paginate_to_next_page(session, current_page + 1)
            session.wait()

    logger.info("Target %s not found → falling back to direct URL", public_identifier)
    return False
