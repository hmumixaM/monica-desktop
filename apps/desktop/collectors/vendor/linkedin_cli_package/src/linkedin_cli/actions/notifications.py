import re
from urllib.parse import urljoin, urlparse

from linkedin_cli.actions.posts import _activity_id, _comment_id
from linkedin_cli.actions.posts import react_to_comment, react_to_post, reply_to_comment
from linkedin_cli.browser.nav import goto_page


def _clean_text(text: str) -> str:
    return " ".join(text.split())


def _notification_url(base_url: str, href: str) -> str:
    parsed = urlparse(urljoin(base_url, href.strip()))
    return parsed._replace(fragment="").geturl()


def _actor_from_text(text: str) -> str | None:
    patterns = [
        r"^(.+?)\s+and\s+\d+\s+others?\s+",
        r"^(.+?)\s+(?:commented|replied|reacted|reposted|posted|is hiring)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1).strip()
    return None


def _notification_from_article(page, article) -> dict | None:
    text = _clean_text(article.inner_text())
    if not text:
        return None

    links = []
    for anchor in article.locator("a[href]").all():
        href = anchor.get_attribute("href")
        if not href:
            continue
        url = _notification_url(page.url, href)
        label = _clean_text(anchor.inner_text()) or None
        links.append({"text": label, "url": url})

    post_link = next((link for link in links if "/feed/update/" in link["url"]), None)
    actor_link = next((link for link in links if "/in/" in link["url"]), None)
    unread = text.startswith("Unread notification.")
    summary = text.removeprefix("Unread notification.").strip() if unread else text

    return {
        "unread": unread,
        "actor": (actor_link["text"] if actor_link and actor_link["text"] else None) or _actor_from_text(summary),
        "text": summary,
        "url": post_link["url"] if post_link else (links[0]["url"] if links else None),
        "activity_id": _activity_id(post_link["url"]) if post_link else None,
        "comment_id": _comment_id(post_link["url"]) if post_link else None,
        "links": links,
    }


def list_notifications(session: "LinkedInSession", *, limit: int = 20) -> dict:
    """Return visible LinkedIn notifications without clicking notification actions."""
    session.ensure_browser()
    goto_page(
        session,
        action=lambda: session.page.goto("https://www.linkedin.com/notifications/", wait_until="domcontentloaded"),
        expected_url_pattern="/notifications/",
        error_message="Failed to open notifications",
    )
    session.wait(1.0, 2.0)

    notifications, seen = [], set()
    for article in session.page.locator("main article").all():
        item = _notification_from_article(session.page, article)
        if not item:
            continue
        key = item.get("url") or item.get("text")
        if not key or key in seen:
            continue
        seen.add(key)
        notifications.append(item)
        if len(notifications) >= limit:
            break
    return {"notifications": notifications}


def _notification_at(session: "LinkedInSession", index: int) -> dict:
    if index < 1:
        raise ValueError("Notification index is 1-based")
    notifications = list_notifications(session, limit=index).get("notifications") or []
    if len(notifications) < index:
        raise RuntimeError(f"Notification {index} is not visible")
    return notifications[index - 1]


def reply_to_notification(session: "LinkedInSession", *, index: int, text: str) -> dict:
    """Reply to the comment referenced by a visible notification."""
    notification = _notification_at(session, index)
    if not notification.get("activity_id") or not notification.get("comment_id"):
        raise RuntimeError("Notification does not reference a replyable comment")
    result = reply_to_comment(
        session,
        notification["activity_id"],
        comment_id=notification["comment_id"],
        author=notification.get("actor"),
        text=text,
    )
    return {**result, "notification": notification, "index": index}


def react_to_notification(session: "LinkedInSession", *, index: int, reaction: str = "like") -> dict:
    """React to the post or comment referenced by a visible notification."""
    notification = _notification_at(session, index)
    if not notification.get("activity_id"):
        raise RuntimeError("Notification does not reference a reactable post")
    if notification.get("comment_id"):
        result = react_to_comment(
            session,
            notification["activity_id"],
            comment_id=notification["comment_id"],
            author=notification.get("actor"),
            reaction=reaction,
        )
    else:
        result = react_to_post(session, notification["activity_id"], reaction=reaction)
    return {**result, "notification": notification, "index": index}
