import re
import time
from urllib.parse import unquote, urljoin, urlparse

from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError

from linkedin_cli.browser.nav import goto_page


ACTIVITY_RE = re.compile(r"urn:li:(?:activity|share):(\d+)|activity-(\d+)")
ADMIN_COMPANY_RE = re.compile(r"/company/(\d+)/admin/?")

SELECTORS = {
    "admin_post": (
        'article, '
        'div[data-urn*=\"urn:li:activity\"], '
        'div[data-id*=\"urn:li:activity\"], '
        'div.feed-shared-update-v2'
    ),
    "composer": 'div[role="dialog"], div[role="alertdialog"], div.artdeco-modal-overlay',
    "editor": (
        'div[role="dialog"] .ql-editor[role="textbox"], '
        'div[role="dialog"] div[role="textbox"][contenteditable="true"], '
        'div[role="dialog"] div[contenteditable="true"]'
    ),
    "start_post": (
        'button:has-text("Start a post"), '
        'button[aria-label*="Start a post" i], '
        'button:has-text("Create a post"), '
        'button[aria-label*="Create a post" i]'
    ),
    "post_button": (
        'div[role="dialog"] button:has-text("Post"), '
        'div[role="dialog"] button[aria-label*="Post" i]'
    ),
    "media_button": (
        'div[role="dialog"] button[aria-label*="Add media" i], '
        'div[role="dialog"] button:has-text("Media")'
    ),
    "document_button": (
        'div[role="dialog"] button[aria-label*="Add a document" i], '
        'div[role="dialog"] button:has-text("Document"), '
        'div[role="dialog"] [role="menuitem"]:has-text("Document")'
    ),
    "poll_button": (
        'div[role="dialog"] button[aria-label*="Create a poll" i], '
        'div[role="dialog"] button:has-text("Create a poll"), '
        'div[role="dialog"] button:has-text("Poll"), '
        'div[role="dialog"] [role="menuitem"]:has-text("Poll")'
    ),
    "more_button": (
        'div[role="dialog"] button[aria-label*="More" i], '
        'div[role="dialog"] button:has-text("More")'
    ),
    "schedule_button": (
        'div[role="dialog"] button[aria-label*="Schedule" i], '
        'div[role="dialog"] button:has-text("Schedule")'
    ),
    "overflow": (
        'button[aria-label*="More" i], '
        'button[aria-label*="Open control menu" i], '
        'button:has-text("More")'
    ),
    "delete_menu": (
        '[role="menuitem"]:has-text("Delete"), '
        'div[role="menu"] button:has-text("Delete"), '
        'div[role="menu"] span:has-text("Delete")'
    ),
    "edit_menu": (
        '[role="menuitem"]:has-text("Edit post"), '
        '[role="menuitem"]:has-text("Edit"), '
        'div[role="menu"] button:has-text("Edit post"), '
        'div[role="menu"] span:has-text("Edit post"), '
        'div[role="menu"] button:has-text("Edit"), '
        'div[role="menu"] span:has-text("Edit")'
    ),
    "confirm_delete": 'button:has-text("Delete"), button[aria-label*="Delete" i]',
    "message": (
        'div.org-inbox-message__container, '
        'li.msg-s-message-list__event, '
        'div.msg-s-event-listitem, '
        'div[componentkey*=message], '
        'div[data-test-id*=message]'
    ),
    "message_editor": (
        'textarea.org-inbox-thread-footer__contenteditable, '
        'div.org-inbox-reply-box__editor [contenteditable="true"], '
        'div.org-inbox-reply-box [contenteditable="true"], '
        'div.org-inbox-thread__container div[role="textbox"][contenteditable="true"], '
        'div.scaffold-layout__detail div[role="textbox"][contenteditable="true"]'
    ),
    "send_button": (
        'div.org-inbox-thread__container button:has-text("Send"), '
        'div.org-inbox-thread__container button[aria-label*="Send" i], '
        'div.scaffold-layout__detail button:has-text("Send"), '
        'div.scaffold-layout__detail button[aria-label*="Send" i]'
    ),
    "message_attach_button": (
        'button[aria-label*="Attach" i], '
        'button[aria-label*="Add attachment" i], '
        'button[title*="Attach" i]'
    ),
    "message_expand_button": (
        'button[aria-label*="Maximize compose field" i], '
        'button:has-text("Maximize compose field")'
    ),
}


def _admin_posts_url(company_id: str, tab: str = "published") -> str:
    return f"https://www.linkedin.com/company/{company_id}/admin/page-posts/{tab}/"


def _admin_url(company_id: str) -> str:
    return f"https://www.linkedin.com/company/{company_id}/admin/"


def _inbox_url(company_id: str) -> str:
    return f"https://www.linkedin.com/company/{company_id}/admin/inbox/"


def _thread_url(company_id: str, thread_id_or_url: str) -> str:
    if thread_id_or_url.startswith(("http://", "https://")):
        return thread_id_or_url
    return f"https://www.linkedin.com/company/{company_id}/admin/inbox/thread/{thread_id_or_url}/"


def _clean_url(base_url: str, href: str) -> str:
    full_url = urljoin(base_url, href.strip())
    parsed = urlparse(full_url)
    return parsed._replace(query="", fragment="").geturl()


def _company_id_from_admin_url(url: str) -> str | None:
    match = ADMIN_COMPANY_RE.search(unquote(url))
    return match.group(1) if match else None


def _activity_id(value: str | None) -> str | None:
    if not value:
        return None
    match = ACTIVITY_RE.search(unquote(value))
    if not match:
        return None
    return next(group for group in match.groups() if group)


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


def _first_int(patterns: list[str], text: str) -> int | None:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return int(match.group(1).replace(",", ""))
    return None


def _engagement_from_text(text: str) -> dict:
    return {
        "reactions": _first_int([r"(\d[\d,]*)\s+reaction", r"(\d[\d,]*)\s+(?:like|likes)"], text),
        "comments": _first_int([r"(\d[\d,]*)\s+comment"], text),
        "reposts": _first_int([r"(\d[\d,]*)\s+repost", r"(\d[\d,]*)\s+share"], text),
        "impressions": _first_int([r"(\d[\d,]*)\s+impression"], text),
    }


def _post_content_from_lines(lines: list[str]) -> str | None:
    stop_words = {
        "Like", "Comment", "Repost", "Send", "Share", "View analytics", "Boost", "More", "Show more", "Show less",
    }
    content = []
    for line in lines:
        lower = line.lower()
        if line.startswith("Feed post number"):
            continue
        if line in stop_words or line.endswith("impressions"):
            continue
        if re.fullmatch(r"\d+[smhdwoyr]o\s*•?", line):
            continue
        if re.search(r"^\d[\d,]*\s+(reaction|comment|repost|share|like|impression)", line, flags=re.IGNORECASE):
            continue
        if " reacted" in lower or re.search(r"\band\s+\d[\d,]*\s+others?", line, flags=re.IGNORECASE):
            continue
        content.append(line)
    return "\n".join(content[:12]).strip() or None


def _post_from_locator(page, locator) -> dict:
    text = locator.inner_text().strip()
    attrs = [locator.get_attribute(name) for name in ("data-urn", "data-id", "id")]
    hrefs = [link.get_attribute("href") for link in locator.locator('a[href*="/feed/update/"]').all()]
    activity_id = next((_activity_id(value) for value in attrs + hrefs if _activity_id(value)), None)
    url = _clean_url(page.url, hrefs[0]) if hrefs else None
    if not url and activity_id:
        url = f"https://www.linkedin.com/feed/update/urn:li:activity:{activity_id}/"
    return {
        "activity_id": activity_id,
        "url": url,
        "content": _post_content_from_lines(_text_lines(text)),
        "engagement": _engagement_from_text(text),
    }


def _visible_page_posts(page, *, limit: int) -> list[dict]:
    posts, seen = [], set()
    for locator in page.locator(SELECTORS["admin_post"]).all():
        if not locator.is_visible():
            continue
        post = _post_from_locator(page, locator)
        key = post.get("activity_id")
        if not key or key in seen:
            continue
        seen.add(key)
        posts.append(post)
        if len(posts) >= limit:
            break
    return posts


def _first_visible(page, selector: str, *, timeout_ms: int = 8_000):
    deadline = time.monotonic() + timeout_ms / 1000
    locator = page.locator(selector)
    while time.monotonic() < deadline:
        for index in range(locator.count()):
            item = locator.nth(index)
            if item.is_visible():
                return item
        time.sleep(0.25)
    locator.first.wait_for(state="visible", timeout=1)
    return locator.first


def _click_first(page, selector: str, *, timeout_ms: int = 8_000) -> bool:
    try:
        _first_visible(page, selector, timeout_ms=timeout_ms).click(force=True)
        return True
    except PlaywrightTimeoutError:
        return False


def _scroll_until_count(page, item_selector: str, *, limit: int, direction: str = "down", timeout_ms: int = 8_000) -> None:
    deadline = time.monotonic() + timeout_ms / 1000
    previous_count = -1
    stable_rounds = 0
    delta = -900 if direction == "up" else 900
    while time.monotonic() < deadline:
        count = page.locator(item_selector).count()
        if count >= limit:
            return
        stable_rounds = stable_rounds + 1 if count == previous_count else 0
        if stable_rounds >= 3:
            return
        previous_count = count
        page.mouse.wheel(0, delta)
        time.sleep(0.5)


def _click_dialog_button(session, labels: list[str], *, timeout_ms: int = 8_000) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        dialogs = session.page.locator(SELECTORS["composer"]).all()
        for dialog in reversed(dialogs):
            if not dialog.is_visible():
                continue
            for button in dialog.locator("button").all():
                text = " ".join((button.inner_text() or "").split())
                aria = button.get_attribute("aria-label") or ""
                if not button.is_visible() or not button.is_enabled():
                    continue
                if text in labels or aria in labels:
                    button.click(force=True)
                    return True
        time.sleep(0.25)
    return False


def _visible_composer_text(session) -> str:
    chunks = []
    for dialog in session.page.locator(SELECTORS["composer"]).all():
        if dialog.is_visible():
            chunks.append(dialog.inner_text())
    for footer in session.page.locator('footer.org-inbox-thread-footer, div.org-inbox-thread-footer__wrapper').all():
        if footer.is_visible():
            chunks.append(footer.inner_text())
            values = footer.locator("textarea, input").evaluate_all("els => els.map(e => e.value || '').join('\\n')")
            if values:
                chunks.append(values)
    return "\n".join(chunks)


def _set_editor_text(session, text: str, selector: str = SELECTORS["editor"]) -> None:
    editor = _first_visible(session.page, selector)
    try:
        editor.scroll_into_view_if_needed(timeout=2_000)
        editor.click(force=True)
        if (editor.evaluate("element => element.tagName") or "").lower() == "textarea":
            editor.fill(text)
        else:
            session.page.keyboard.insert_text(text)
    except PlaywrightError:
        editor.evaluate(
            """(element, value) => {
                element.focus();
                if (element.tagName === "TEXTAREA" || element.tagName === "INPUT") {
                    element.value = value;
                } else {
                    element.textContent = value;
                }
                element.dispatchEvent(new InputEvent("input", {bubbles: true, inputType: "insertText", data: value}));
            }""",
            text,
        )
    session.wait(0.5, 1.0)
    if text in _visible_composer_text(session):
        return
    editor.evaluate(
        """(element, value) => {
            if (element.tagName === "TEXTAREA" || element.tagName === "INPUT") {
                element.value = value;
            } else {
                element.textContent = value;
            }
            element.dispatchEvent(new InputEvent("input", {bubbles: true, inputType: "insertText", data: value}));
        }""",
        text,
    )
    session.wait(0.5, 1.0)
    if text not in _visible_composer_text(session):
        raise RuntimeError("Could not enter text into LinkedIn page editor")


def _upload_files(session, button_selector: str, files: list[str]) -> bool:
    from linkedin_cli.actions.posts import _upload_files as upload_files

    return upload_files(session, button_selector, files)


def _add_poll(session, question: str | None, options: list[str] | None) -> bool:
    from linkedin_cli.actions.posts import _add_poll as add_poll

    return add_poll(session, question, options)


def list_page_posts(session: "LinkedInSession", company_id: str, *, limit: int = 10) -> dict:
    """Return visible published company page admin posts."""
    session.ensure_browser()
    tab = "published"
    url = _admin_posts_url(company_id, tab)
    goto_page(
        session,
        action=lambda: session.page.goto(url, wait_until="domcontentloaded"),
        expected_url_pattern=f"/company/{company_id}/admin/page-posts/",
        error_message="Failed to open company page posts admin",
    )
    return {"company_id": company_id, "tab": tab, "posts": _visible_page_posts(session.page, limit=limit)}


def _open_page_scheduled_posts(session: "LinkedInSession", company_id: str) -> None:
    _open_page_post_composer(session, company_id)
    if not _click_first(session.page, SELECTORS["schedule_button"]):
        raise RuntimeError("Could not open LinkedIn page schedule controls")
    session.wait(1.0, 2.0)

    view_all = session.page.locator('div[role="dialog"] button:has-text("View all scheduled posts")').first
    if not view_all.is_visible():
        raise RuntimeError("Could not open LinkedIn page scheduled posts list")
    view_all.click(force=True)
    session.wait(2.0, 4.0)


def list_admin_pages(session: "LinkedInSession") -> dict:
    """Return company pages the current session can administer, discovered from the feed."""
    session.ensure_browser()
    goto_page(
        session,
        action=lambda: session.page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded"),
        expected_url_pattern="/feed/",
        error_message="Failed to open LinkedIn feed",
    )
    pages, seen = [], set()
    for link in session.page.locator('a[href*="/company/"][href*="/admin"]').all():
        href = link.get_attribute("href") or ""
        url = _clean_url(session.page.url, href)
        company_id = _company_id_from_admin_url(url)
        if not company_id or company_id in seen:
            continue
        seen.add(company_id)
        name = " ".join(_text_lines(link.inner_text())) or None
        pages.append({"company_id": company_id, "name": name, "admin_url": _admin_url(company_id)})
    return {"pages": pages}


def _open_page_post_composer(session: "LinkedInSession", company_id: str) -> None:
    list_page_posts(session, company_id, limit=1)
    if not _click_first(session.page, SELECTORS["start_post"]):
        raise RuntimeError("Could not open LinkedIn page post composer")
    _first_visible(session.page, SELECTORS["composer"])


def create_page_post(
    session: "LinkedInSession",
    company_id: str,
    text: str,
    *,
    images: list[str] | None = None,
    documents: list[str] | None = None,
    poll_question: str | None = None,
    poll_options: list[str] | None = None,
) -> dict:
    """Create a company page post with optional images, documents, or poll."""
    if not text.strip():
        raise ValueError("Post text cannot be empty")
    if poll_options and (images or documents):
        raise ValueError("LinkedIn posts cannot combine polls with image/document uploads")
    _open_page_post_composer(session, company_id)
    _set_editor_text(session, text)
    uploaded_images = _upload_files(session, SELECTORS["media_button"], images or [])
    uploaded_documents = _upload_files(session, SELECTORS["document_button"], documents or [])
    added_poll = _add_poll(session, poll_question or text, poll_options)
    if not _click_dialog_button(session, ["Post"]):
        raise RuntimeError("Could not find LinkedIn page post submit button")
    session.wait(2.0, 4.0)
    return {
        "company_id": company_id,
        "posted": True,
        "text": text,
        "images": images or [],
        "documents": documents or [],
        "poll": {"question": poll_question, "options": poll_options or []} if added_poll else None,
        "uploaded_images": uploaded_images,
        "uploaded_documents": uploaded_documents,
    }


def schedule_page_post(session: "LinkedInSession", company_id: str, text: str, scheduled_at: str) -> dict:
    """Schedule a company page text post for an ISO local datetime."""
    if not text.strip():
        raise ValueError("Post text cannot be empty")

    for _ in range(5):
        dialogs = session.page.locator(SELECTORS["composer"]).all()
        visible = [dialog for dialog in dialogs if dialog.is_visible()]
        if not visible:
            break
        session.page.keyboard.press("Escape")
        session.wait(0.5, 1.0)

    from linkedin_cli.actions.posts import _schedule_time

    _open_page_post_composer(session, company_id)
    _set_editor_text(session, text)
    _schedule_time(session, scheduled_at)
    if not _click_dialog_button(session, ["Post", "Schedule"]):
        raise RuntimeError("Could not find LinkedIn page schedule submit button")
    session.wait(2.0, 4.0)
    return {"company_id": company_id, "scheduled": True, "scheduled_at": scheduled_at, "text": text}


def list_page_scheduled_posts(session: "LinkedInSession", company_id: str) -> dict:
    """List scheduled company page posts from the admin scheduled tab."""
    from linkedin_cli.actions.posts import list_scheduled_posts

    result = list_scheduled_posts(session, open_scheduled_posts=lambda s: _open_page_scheduled_posts(s, company_id))
    return {"company_id": company_id, **result}


def cancel_page_scheduled_post(session: "LinkedInSession", company_id: str, index: int) -> dict:
    """Cancel a scheduled company page post by 1-based index."""
    from linkedin_cli.actions.posts import cancel_scheduled_post

    result = cancel_scheduled_post(session, index, open_scheduled_posts=lambda s: _open_page_scheduled_posts(s, company_id))
    return {"company_id": company_id, **result}


def update_page_scheduled_post_time(session: "LinkedInSession", company_id: str, index: int, scheduled_at: str | None = None, *, text: str | None = None) -> dict:
    """Update the scheduled datetime and/or text for a company page post by 1-based index."""
    from linkedin_cli.actions.posts import update_scheduled_post_time

    result = update_scheduled_post_time(session, index, scheduled_at, text=text, open_scheduled_posts=lambda s: _open_page_scheduled_posts(s, company_id))
    return {"company_id": company_id, **result}


def delete_page_post(session: "LinkedInSession", company_id: str, post_id_or_url: str) -> dict:
    """Delete a company page post by opening it and using the visible admin menu."""
    session.ensure_browser()
    url = post_id_or_url if post_id_or_url.startswith(("http://", "https://")) else f"https://www.linkedin.com/feed/update/urn:li:activity:{post_id_or_url}/"
    goto_page(
        session,
        action=lambda: session.page.goto(url, wait_until="domcontentloaded"),
        expected_url_pattern="/feed/update/",
        error_message="Failed to open company page post",
    )
    activity_id = _activity_id(session.page.url) or _activity_id(post_id_or_url)
    if not _click_first(session.page, SELECTORS["overflow"]):
        raise RuntimeError("Could not open LinkedIn page post action menu")
    if not _click_first(session.page, SELECTORS["delete_menu"]):
        raise RuntimeError("Could not find LinkedIn page post delete action")
    if not _click_first(session.page, SELECTORS["confirm_delete"]):
        raise RuntimeError("Could not confirm LinkedIn page post deletion")
    session.wait(2.0, 4.0)
    return {"company_id": company_id, "activity_id": activity_id, "deleted": True}


def edit_page_post(session: "LinkedInSession", company_id: str, post_id_or_url: str, text: str) -> dict:
    """Edit the text content of an already-published company page post."""
    if not text.strip():
        raise ValueError("Post text cannot be empty")
    session.ensure_browser()
    url = post_id_or_url if post_id_or_url.startswith(("http://", "https://")) else f"https://www.linkedin.com/feed/update/urn:li:activity:{post_id_or_url}/"
    goto_page(
        session,
        action=lambda: session.page.goto(url, wait_until="domcontentloaded"),
        expected_url_pattern="/feed/update/",
        error_message="Failed to open company page post",
    )
    activity_id = _activity_id(session.page.url) or _activity_id(post_id_or_url)
    if not _click_first(session.page, SELECTORS["overflow"]):
        raise RuntimeError("Could not open LinkedIn page post action menu")
    if not _click_first(session.page, SELECTORS["edit_menu"]):
        raise RuntimeError("Could not find LinkedIn page post edit action")

    from linkedin_cli.actions.posts import _replace_text

    _first_visible(session.page, SELECTORS["composer"])
    _replace_text(session, text)
    if not _click_dialog_button(session, ["Save", "Post"], timeout_ms=8_000):
        raise RuntimeError("Could not save LinkedIn page post edits")
    session.wait(2.0, 4.0)
    return {"company_id": company_id, "activity_id": activity_id, "updated": True, "text": text}


def _thread_id_from_url(url: str) -> str | None:
    match = re.search(r"/admin/inbox/thread/([^/?#]+)/?", url)
    return unquote(match.group(1)) if match else None


def _message_from_locator(locator) -> dict | None:
    lines = _text_lines(locator.inner_text())
    if not lines:
        return None
    if "org-inbox-message__container" in (locator.get_attribute("class") or ""):
        sender_line = " ".join(_text_lines(locator.locator(".org-inbox-message__sender-info").first.inner_text())) if locator.locator(".org-inbox-message__sender-info").count() else lines[0]
        text_lines = _text_lines(locator.locator(".org-inbox-message__content-container").first.inner_text()) if locator.locator(".org-inbox-message__content-container").count() else lines[1:]
        attachments = _message_attachments_from_locator(locator)
        for attachment in attachments:
            for value in (attachment.get("name"), attachment.get("size"), "Download"):
                if value in text_lines:
                    text_lines.remove(value)
        sender = re.sub(r"\s+\d{1,2}:\d{2}\s*(?:AM|PM)\s*$", "", sender_line, flags=re.IGNORECASE).strip()
        timestamp = sender_line.removeprefix(sender).strip() or None
        return {"sender": sender or None, "timestamp": timestamp, "text": "\n".join(text_lines).strip() or None, "attachments": attachments}
    return {"sender": lines[0], "text": "\n".join(lines[1:]).strip() or None, "attachments": []}


def _message_attachments_from_locator(locator) -> list[dict]:
    attachments = []
    for link in locator.locator("a.org-message-attachment__download-attachment").all():
        lines = _text_lines(link.inner_text())
        if not lines:
            continue
        attachments.append({
            "type": "file",
            "name": lines[0],
            "size": next((line for line in lines[1:] if re.search(r"\b(?:B|KB|MB|GB)\b", line)), None),
            "url": urljoin("https://www.linkedin.com", link.get_attribute("href") or ""),
            "downloadable": any(line.lower() == "download" for line in lines),
        })
    return attachments


def list_page_inbox(session: "LinkedInSession", company_id: str, *, limit: int = 20) -> dict:
    """Return visible company page inbox threads."""
    session.ensure_browser()
    goto_page(
        session,
        action=lambda: session.page.goto(_inbox_url(company_id), wait_until="domcontentloaded"),
        expected_url_pattern=f"/company/{company_id}/admin/inbox/",
        error_message="Failed to open company page inbox",
    )
    _scroll_until_count(session.page, 'a[href*="/admin/inbox/thread/"]', limit=limit, direction="down")
    threads, seen = [], set()
    for link in session.page.locator('a[href*="/admin/inbox/thread/"]').all():
        href = link.get_attribute("href") or ""
        url = _clean_url(session.page.url, href)
        thread_id = _thread_id_from_url(url)
        if not thread_id or thread_id in seen:
            continue
        seen.add(thread_id)
        lines = _text_lines(link.inner_text())
        threads.append({"thread_id": thread_id, "url": url, "summary": " | ".join(lines[:4]) or None})
        if len(threads) >= limit:
            break
    return {"company_id": company_id, "threads": threads}


def page_inbox_thread(session: "LinkedInSession", company_id: str, thread_id_or_url: str, *, limit: int = 50) -> dict:
    """Return visible messages in a company page inbox thread."""
    session.ensure_browser()
    url = _thread_url(company_id, thread_id_or_url)
    goto_page(
        session,
        action=lambda: session.page.goto(url, wait_until="domcontentloaded"),
        expected_url_pattern=f"/company/{company_id}/admin/inbox/thread/",
        error_message="Failed to open company page inbox thread",
    )
    org_message_selector = 'div.org-inbox-message__container'
    _scroll_until_count(session.page, org_message_selector, limit=limit, direction="up")
    message_selector = org_message_selector if session.page.locator(org_message_selector).count() else SELECTORS["message"]
    messages = []
    for locator in session.page.locator(message_selector).all():
        if not locator.is_visible():
            continue
        message = _message_from_locator(locator)
        if not message:
            continue
        messages.append(message)
        if len(messages) >= limit:
            break
    return {
        "company_id": company_id,
        "thread_id": _thread_id_from_url(session.page.url) or thread_id_or_url,
        "messages": messages,
    }


def _attach_message_files(session, files: list[str]) -> bool:
    if not files:
        return False
    from linkedin_cli.actions.posts import _existing_paths

    paths = _existing_paths(files)
    page = session.page
    inputs = page.locator('input[type="file"]')
    admin_inputs = page.locator('footer.org-inbox-thread-footer input[type="file"], div.org-inbox-thread-footer__wrapper input[type="file"]')
    if admin_inputs.count() > 0:
        admin_inputs.last.set_input_files(paths)
    elif inputs.count() > 0:
        inputs.last.set_input_files(paths)
    else:
        with page.expect_file_chooser(timeout=5_000) as chooser_info:
            if not _click_first(page, SELECTORS["message_attach_button"], timeout_ms=5_000):
                raise RuntimeError("Could not open LinkedIn page inbox attachment controls")
        chooser_info.value.set_files(paths)
    session.wait(1.0, 2.0)
    return True


def reply_page_inbox_thread(session: "LinkedInSession", company_id: str, thread_id_or_url: str, text: str, *, attachments: list[str] | None = None) -> dict:
    """Reply to a company page inbox thread."""
    if not text.strip():
        raise ValueError("Reply text cannot be empty")
    thread = page_inbox_thread(session, company_id, thread_id_or_url, limit=1)
    _click_first(session.page, SELECTORS["message_expand_button"], timeout_ms=2_000)
    session.wait(0.5, 1.0)
    _set_editor_text(session, text, SELECTORS["message_editor"])
    attached = _attach_message_files(session, attachments or [])
    if not _click_first(session.page, SELECTORS["send_button"]):
        raise RuntimeError("Could not send LinkedIn page inbox reply")
    session.wait(1.0, 2.0)
    return {"company_id": company_id, "thread_id": thread.get("thread_id"), "sent": True, "text": text, "attachments": attachments or [], "attached": attached}
