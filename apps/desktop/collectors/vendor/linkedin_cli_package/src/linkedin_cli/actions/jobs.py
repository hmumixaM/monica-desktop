import logging
import re
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse

from linkedin_cli.browser.nav import goto_page

logger = logging.getLogger(__name__)

JOB_ID_RE = re.compile(r"/jobs/view/(\d+)")

DATE_POSTED_FILTERS = {
    "past-24h": "r86400",
    "past-week": "r604800",
    "past-month": "r2592000",
}

JOB_TYPE_FILTERS = {
    "full-time": "F",
    "part-time": "P",
    "contract": "C",
    "temporary": "T",
    "internship": "I",
}

SELECTORS = {
    "job_links": 'a[href*="/jobs/view/"]',
    "save": (
        'button[aria-label*="Save"][aria-label*="job" i], '
        'button:has-text("Save")'
    ),
    "saved": (
        'button[aria-label*="Unsave" i], '
        'button:has-text("Saved")'
    ),
    "easy_apply": (
        'button[aria-label*="Easy Apply" i], '
        'button:has-text("Easy Apply")'
    ),
    "external_apply": (
        'a[aria-label*="Apply" i], '
        'a:has-text("Apply")'
    ),
    "submit_application": (
        'button[aria-label*="Submit application" i], '
        'button:has-text("Submit application")'
    ),
    "next": (
        'button[aria-label*="Continue to next step" i], '
        'button[aria-label*="Next" i], '
        'button:has-text("Next")'
    ),
    "modal": 'div[role="dialog"]',
}


def _job_id_from_url(url: str) -> str | None:
    match = JOB_ID_RE.search(url)
    return match.group(1) if match else None


def _job_url(job_id_or_url: str) -> str:
    if job_id_or_url.startswith("http://") or job_id_or_url.startswith("https://"):
        return job_id_or_url
    if job_id_or_url.isdigit():
        return f"https://www.linkedin.com/jobs/view/{job_id_or_url}/"
    raise ValueError(f"Expected a LinkedIn job id or URL, got {job_id_or_url!r}")


def _search_url(
    keywords: str,
    *,
    location: str | None = None,
    page: int = 1,
    easy_apply: bool = False,
    remote: bool = False,
    date_posted: str | None = None,
    job_type: str | None = None,
) -> str:
    params = {"keywords": keywords, "origin": "JOB_SEARCH_PAGE_JOB_FILTER"}
    if location:
        params["location"] = location
    if easy_apply:
        params["f_AL"] = "true"
    if remote:
        params["f_WT"] = "2"
    if date_posted:
        params["f_TPR"] = DATE_POSTED_FILTERS[date_posted]
    if job_type:
        params["f_JT"] = JOB_TYPE_FILTERS[job_type]
    if page > 1:
        # LinkedIn jobs search uses a zero-based result offset in increments of 25.
        params["start"] = str((page - 1) * 25)
    return "https://www.linkedin.com/jobs/search/?" + urlencode(params)


def _clean_url(base_url: str, href: str) -> str:
    full_url = urljoin(base_url, href.strip())
    parsed = urlparse(full_url)
    return parsed._replace(query="", fragment="").geturl()


def _text_lines(text: str) -> list[str]:
    seen = set()
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line in seen:
            continue
        seen.add(line)
        lines.append(line)
    return lines


def _job_from_link(page, link) -> dict | None:
    href = link.get_attribute("href") or ""
    job_id = _job_id_from_url(href)
    if not job_id:
        return None

    card = link.locator("xpath=ancestor::li[1]")
    lines = _text_lines(card.inner_text() if card.count() > 0 else link.inner_text())
    title = lines[0] if lines else link.inner_text().strip()
    details = [line for line in lines[1:] if title and not line.startswith(title)]

    return {
        "job_id": job_id,
        "url": _clean_url(page.url, href),
        "title": title,
        "company": details[0] if len(details) > 0 else None,
        "location": details[1] if len(details) > 1 else None,
        "listed": details[-1] if len(details) > 2 else None,
    }


def _first_apply_link(session) -> str | None:
    safety_link = _first_safety_apply_link(session)
    if safety_link:
        return safety_link

    fallback = None
    for link in session.page.locator(SELECTORS["external_apply"]).all():
        label = " ".join(filter(None, [link.inner_text(), link.get_attribute("aria-label") or "", link.get_attribute("href") or ""]))
        href = link.get_attribute("href")
        if href and "apply" in label.lower():
            apply_url = urljoin(session.page.url, href)
            if _apply_method(apply_url) == "easy_apply" or "/safety/go/" in urlparse(apply_url).path:
                return apply_url
            fallback = apply_url

    if fallback and _job_id_from_url(fallback) == _job_id_from_url(session.page.url):
        session.page.locator(SELECTORS["external_apply"]).first.click()
        session.wait()
        safety_link = _first_safety_apply_link(session)
        if safety_link:
            return safety_link
    return fallback


def _first_safety_apply_link(session) -> str | None:
    for link in session.page.locator('a[href*="/safety/go/"]').all():
        href = link.get_attribute("href")
        if href:
            return urljoin(session.page.url, href)
    return None


def _apply_method(apply_url: str | None) -> str | None:
    if not apply_url:
        return None
    parsed = urlparse(apply_url)
    if parsed.path.endswith("/apply/") or parse_qs(parsed.query).get("openSDUIApplyFlow"):
        return "easy_apply"
    if "/safety/go/" in parsed.path:
        return "external"
    return "external"


def _external_url(apply_url: str | None) -> str | None:
    if not apply_url:
        return None
    parsed = urlparse(apply_url)
    if "/safety/go/" not in parsed.path:
        return None
    url = parse_qs(parsed.query).get("url", [None])[0]
    return unquote(url) if url else None


def _ats(external_url: str | None) -> str | None:
    if not external_url:
        return None
    host = urlparse(external_url).netloc.lower()
    if "greenhouse.io" in host:
        return "greenhouse"
    if "lever.co" in host:
        return "lever"
    if "myworkdayjobs.com" in host or "workdayjobs.com" in host:
        return "workday"
    if "smartrecruiters.com" in host:
        return "smartrecruiters"
    if "ashbyhq.com" in host:
        return "ashby"
    if "icims.com" in host:
        return "icims"
    if "successfactors" in host:
        return "successfactors"
    return "company_site"


def _apply_metadata(apply_url: str | None) -> dict:
    external_url = _external_url(apply_url)
    return {"external_url": external_url, "ats": _ats(external_url)}


def _job_detail_text(session) -> str:
    main = session.page.locator("main")
    if main.count() > 0:
        return main.first.inner_text().strip()
    return session.page.locator("body").inner_text().strip()


def _job_detail_lines(session) -> list[str]:
    return _text_lines(_job_detail_text(session))


def _description_from_lines(lines: list[str]) -> str | None:
    if "About the job" not in lines:
        return None
    start = lines.index("About the job") + 1
    end_markers = {
        "Show more",
        "Show less",
        "Skills",
        "Seniority level",
        "Employment type",
        "Job function",
        "Industries",
        "Benefits found in job post",
        "Set alert for similar jobs",
        "Job search faster with Premium",
        "About the company",
        "Interested in working with us in the future?",
    }
    desc = []
    for line in lines[start:]:
        if line in end_markers:
            break
        desc.append(line)
    return "\n".join(desc).strip() or None


def show_job(session: "LinkedInSession", job_id_or_url: str) -> dict:
    """Return structured details from a LinkedIn job detail page."""
    job = open_job(session, job_id_or_url)
    lines = _job_detail_lines(session)
    apply_url = _first_apply_link(session)
    apply_metadata = _apply_metadata(apply_url)

    company = lines[0] if len(lines) > 0 else None
    title = lines[1] if len(lines) > 1 else None
    meta = lines[2] if len(lines) > 2 else ""
    meta_parts = [part.strip() for part in meta.split("·") if part.strip()]

    saved = session.page.locator(SELECTORS["saved"]).count() > 0
    badges = [line for line in lines[:12] if line in {"Remote", "Hybrid", "On-site", "Full-time", "Part-time", "Contract", "Temporary", "Internship"}]

    return {
        **job,
        "title": title,
        "company": company,
        "location": meta_parts[0] if meta_parts else None,
        "listed": meta_parts[1] if len(meta_parts) > 1 else None,
        "applicants": meta_parts[2] if len(meta_parts) > 2 else None,
        "workplace": next((b for b in badges if b in {"Remote", "Hybrid", "On-site"}), None),
        "employment_type": next((b for b in badges if b in {"Full-time", "Part-time", "Contract", "Temporary", "Internship"}), None),
        "saved": saved,
        "apply_method": _apply_method(apply_url),
        "apply_url": apply_url,
        **apply_metadata,
        "description": _description_from_lines(lines),
    }


def search_jobs(
    session: "LinkedInSession",
    keywords: str,
    *,
    location: str | None = None,
    page: int = 1,
    easy_apply: bool = False,
    remote: bool = False,
    date_posted: str | None = None,
    job_type: str | None = None,
) -> dict:
    """Search LinkedIn Jobs; return the visible result cards as a structured envelope."""
    session.ensure_browser()
    goto_page(
        session,
        action=lambda: session.page.goto(_search_url(
            keywords,
            location=location,
            page=page,
            easy_apply=easy_apply,
            remote=remote,
            date_posted=date_posted,
            job_type=job_type,
        )),
        expected_url_pattern="/jobs/search/",
        error_message="Failed to reach Jobs search results",
    )

    jobs, seen = [], set()
    for link in session.page.locator(SELECTORS["job_links"]).all():
        job = _job_from_link(session.page, link)
        if not job or job["job_id"] in seen:
            continue
        seen.add(job["job_id"])
        jobs.append(job)

    return {
        "query": keywords,
        "location": location,
        "page": page,
        "filters": {
            "easy_apply": easy_apply,
            "remote": remote,
            "date_posted": date_posted,
            "job_type": job_type,
        },
        "jobs": jobs,
    }


def saved_jobs(session: "LinkedInSession", *, page: int = 1, card_type: str = "saved", limit: int = 10) -> dict:
    """Return the visible jobs from LinkedIn's saved jobs page."""
    from linkedin_cli.actions.saved_jobs import list_saved_jobs

    return list_saved_jobs(session, page=page, card_type=card_type, limit=limit)


def open_job(session: "LinkedInSession", job_id_or_url: str) -> dict:
    """Navigate to a job detail page and return its canonical id/url."""
    session.ensure_browser()
    url = _job_url(job_id_or_url)
    goto_page(
        session,
        action=lambda: session.page.goto(url, wait_until="domcontentloaded"),
        expected_url_pattern="/jobs/view/",
        error_message="Failed to open job",
    )
    job_id = _job_id_from_url(session.page.url) or _job_id_from_url(url)
    return {"job_id": job_id, "url": _clean_url(session.page.url, session.page.url)}


def save_job(session: "LinkedInSession", job_id_or_url: str) -> dict:
    """Save a LinkedIn job, no-oping if it is already saved."""
    job = open_job(session, job_id_or_url)

    if session.page.locator(SELECTORS["saved"]).count() > 0:
        return {**job, "saved": True, "changed": False}

    save = session.page.locator(SELECTORS["save"])
    if save.count() == 0:
        return {**job, "saved": False, "changed": False}

    save.first.click()
    session.wait()
    saved = session.page.locator(SELECTORS["saved"]).count() > 0
    return {**job, "saved": saved, "changed": saved}


def unsave_job(session: "LinkedInSession", jobs: list[str]) -> dict:
    """Unsave LinkedIn jobs from the saved jobs page."""
    from linkedin_cli.actions.saved_jobs import unsave_saved_job, unsave_saved_jobs

    if len(jobs) == 1:
        return unsave_saved_job(session, jobs[0])
    return unsave_saved_jobs(session, jobs)


def apply_job(session: "LinkedInSession", job_id_or_url: str, *, submit: bool = False) -> dict:
    """Start a job application.

    External apply jobs return the outbound LinkedIn safety URL. Easy Apply jobs
    are only submitted when ``submit`` is true and the first dialog already has a
    final Submit button, avoiding blind multi-step form automation.
    """
    job = open_job(session, job_id_or_url)

    apply_url = _first_apply_link(session)
    apply_method = _apply_method(apply_url)
    easy_apply = session.page.locator(SELECTORS["easy_apply"])

    if apply_method == "easy_apply" or easy_apply.count() > 0:
        if apply_url:
            goto_page(
                session,
                action=lambda: session.page.goto(apply_url, wait_until="domcontentloaded"),
                expected_url_pattern="/jobs/view/",
                error_message="Failed to open Easy Apply flow",
            )
        else:
            easy_apply.first.click()
            session.wait()

        modal = session.page.locator(SELECTORS["modal"])
        scope = modal.first if modal.count() > 0 else session.page
        submit_button = scope.locator(SELECTORS["submit_application"])
        next_button = scope.locator(SELECTORS["next"])

        if submit and submit_button.count() > 0:
            submit_button.first.click()
            session.wait()
            return {**job, "method": "easy_apply", "submitted": True, "manual": False}

        return {
            **job,
            "method": "easy_apply",
            "submitted": False,
            "manual": submit_button.count() == 0,
            "apply_url": apply_url,
            **_apply_metadata(apply_url),
            "next_step": "submit" if submit_button.count() > 0 else "complete_form" if next_button.count() > 0 else "review_modal",
        }

    if apply_url:
        return {
            **job,
            "method": apply_method or "external",
            "submitted": False,
            "manual": True,
            "apply_url": apply_url,
            **_apply_metadata(apply_url),
        }

    return {**job, "method": None, "submitted": False, "manual": True}
