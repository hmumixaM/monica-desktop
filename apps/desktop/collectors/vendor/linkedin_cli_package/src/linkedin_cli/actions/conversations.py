# linkedin/actions/conversations.py
"""Retrieve past LinkedIn conversations."""
import logging
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

from linkedin_cli.api.client import PlaywrightLinkedinAPI
from linkedin_cli.api.messaging import fetch_conversations, fetch_messages, encode_urn
from linkedin_cli.browser.nav import goto_page

logger = logging.getLogger(__name__)


def _messaging_url() -> str:
    return "https://www.linkedin.com/messaging/"


def _timestamp(ms: int | None) -> str:
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _localized_text(value) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return ""
    if isinstance(value.get("text"), str):
        return value["text"]
    for key in ("attributesV2", "attributes"):
        for item in value.get(key) or []:
            text = _localized_text(item)
            if text:
                return text
    return ""


def _participant_name(participant: dict) -> str:
    member = (participant.get("participantType") or {}).get("member") or {}
    profile = member.get("profile") or {}
    name = _localized_text(profile.get("miniProfileName"))
    if name:
        return name
    first = _localized_text(member.get("firstName"))
    last = _localized_text(member.get("lastName"))
    return " ".join(part for part in (first, last) if part) or participant.get("hostIdentityUrn") or "unknown"


def _participant_public_identifier(participant: dict) -> str | None:
    member = (participant.get("participantType") or {}).get("member") or {}
    profile = member.get("profile") or {}
    return profile.get("publicIdentifier") or member.get("publicIdentifier")


def _conversation_id(conversation_url: str | None, entity_urn: str | None) -> str | None:
    if conversation_url:
        path_parts = [part for part in urlparse(conversation_url).path.split("/") if part]
        if path_parts:
            return path_parts[-1]
    if not entity_urn:
        return None
    return entity_urn.rsplit(":", 1)[-1]


def conversation_urn_from_thread_id(thread_id: str, mailbox_urn: str) -> str:
    if thread_id.startswith("urn:li:msg_conversation:"):
        return thread_id
    return f"urn:li:msg_conversation:({mailbox_urn},{thread_id})"


def _message_text(message: dict) -> str:
    body = message.get("body") or {}
    if isinstance(body, dict):
        text = body.get("text")
        if isinstance(text, str):
            return text
    return _localized_text(body)


def _conversation_summary(conv: dict, mailbox_urn: str) -> dict:
    participants = []
    for participant in conv.get("conversationParticipants") or []:
        host_urn = participant.get("hostIdentityUrn")
        if host_urn == mailbox_urn:
            continue
        participants.append({
            "name": _participant_name(participant),
            "public_identifier": _participant_public_identifier(participant),
            "urn": host_urn,
        })

    entity_urn = conv.get("entityUrn")
    conversation_url = conv.get("conversationUrl")
    thread_id = _conversation_id(conversation_url, entity_urn)
    last_activity_at = conv.get("lastActivityAt") or conv.get("lastModifiedAt")
    messages = ((conv.get("messages") or {}).get("elements") or [])
    last_message = conv.get("lastMessage") or conv.get("lastMessageEvent") or (messages[0] if messages else {})

    return {
        "thread_id": thread_id,
        "entity_urn": entity_urn,
        "url": urljoin("https://www.linkedin.com", conversation_url) if conversation_url else None,
        "participants": participants,
        "last_message": _message_text(last_message),
        "last_activity_at": _timestamp(last_activity_at),
        "unread_count": conv.get("unreadCount") or 0,
    }


def _file_attachment(file_data: dict) -> dict:
    return {
        "type": "file",
        "name": file_data.get("name"),
        "media_type": file_data.get("mediaType"),
        "byte_size": file_data.get("byteSize"),
        "asset_urn": file_data.get("assetUrn"),
        "url": file_data.get("url"),
    }


def _message_attachments(msg: dict) -> list[dict]:
    attachments = []
    for item in msg.get("renderContent") or msg.get("renderContentUnions") or []:
        if not isinstance(item, dict):
            continue
        file_data = item.get("file")
        if isinstance(file_data, dict):
            attachments.append(_file_attachment(file_data))
    return attachments


def find_conversation_urn(api: PlaywrightLinkedinAPI, target_urn: str, mailbox_urn: str) -> str | None:
    """Find conversation URN for a target profile URN by scanning recent conversations."""
    for start in range(0, 100, 20):
        raw = fetch_conversations(api, mailbox_urn, count=20, start=start)
        batch = raw.get("data", {}).get("messengerConversationsBySyncToken", {}).get("elements", [])
        if not batch:
            break

        for conv in batch:
            for p in conv.get("conversationParticipants", []):
                if p.get("hostIdentityUrn") == target_urn:
                    return conv.get("entityUrn")
    return None


def find_conversation_urn_via_navigation(session, target_urn: str) -> str | None:
    """Navigate to the messaging page for a profile and capture the conversation URN.

    Works for older conversations not in the first page of API results.
    """
    page = session.page
    captured_urn = [None]

    def on_response(response):
        if "messengerMessages" not in response.url:
            return
        try:
            data = response.json()
            elements = data.get("data", {}).get("messengerMessagesBySyncToken", {}).get("elements", [])
            if elements:
                captured_urn[0] = elements[0].get("conversation", {}).get("entityUrn")
        except Exception:
            pass

    session.context.on("response", on_response)
    try:
        url = f"https://www.linkedin.com/messaging/thread/new/?recipient={encode_urn(target_urn)}"
        logger.debug("Navigating to messaging thread → %s", url)
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(8_000)
    except Exception as e:
        logger.warning("Navigation to messaging thread failed: %s", e)
    finally:
        session.context.remove_listener("response", on_response)

    return captured_urn[0]


def list_conversations(session, *, limit: int = 20) -> dict:
    """Return recent personal messaging conversations."""
    session.ensure_browser()
    goto_page(
        session,
        action=lambda: session.page.goto(_messaging_url(), wait_until="domcontentloaded"),
        expected_url_pattern="/messaging/",
        error_message="Failed to open LinkedIn messaging",
    )

    api = PlaywrightLinkedinAPI(session=session)
    mailbox_urn = session.self_profile["urn"]
    limit = max(limit, 1)
    conversations = []

    for start in range(0, limit, 20):
        raw = fetch_conversations(api, mailbox_urn, count=min(20, limit - start), start=start)
        batch = raw.get("data", {}).get("messengerConversationsBySyncToken", {}).get("elements", [])
        if not batch:
            break
        conversations.extend(_conversation_summary(conv, mailbox_urn) for conv in batch)
        if len(conversations) >= limit:
            break

    return {"conversations": conversations[:limit]}


def parse_message_element(msg: dict) -> dict | None:
    """Parse a single Voyager message element into a dict.

    Returns {entityUrn, text, sender_name, sender_host_urn, delivered_at, is_outgoing (unset)}
    or None if the element should be skipped.
    """
    body = msg.get("body", {})
    text = body.get("text", "") if isinstance(body, dict) else str(body)
    if not text:
        return None

    sender = msg.get("sender", {})
    participant = sender.get("participantType", {}).get("member", {})
    first = (participant.get("firstName") or {}).get("text", "")
    last = (participant.get("lastName") or {}).get("text", "")
    sender_name = f"{first} {last}".strip() or "unknown"

    delivered_at = msg.get("deliveredAt")
    ts = (
        datetime.fromtimestamp(delivered_at / 1000, tz=timezone.utc)
        if delivered_at
        else None
    )

    return {
        "entityUrn": msg.get("entityUrn"),
        "text": text,
        "attachments": _message_attachments(msg),
        "sender_name": sender_name,
        "sender_host_urn": sender.get("hostIdentityUrn", ""),
        "delivered_at": ts,
    }


def parse_messages(raw: dict) -> list[dict]:
    """Parse raw messages response into a list of {sender, text, timestamp} dicts."""
    elements = raw.get("data", {}).get("messengerMessagesBySyncToken", {}).get("elements", [])

    messages = []
    for msg in elements:
        parsed = parse_message_element(msg)
        if not parsed:
            continue
        ts = parsed["delivered_at"]
        messages.append({
            "sender": parsed["sender_name"],
            "text": parsed["text"],
            "attachments": parsed["attachments"],
            "timestamp": ts.strftime("%Y-%m-%d %H:%M") if ts else "",
        })

    messages.sort(key=lambda m: m["timestamp"])
    return messages


def get_conversation(session, target_urn: str, mailbox_urn: str, *, limit: int = 50) -> list[dict] | None:
    """Retrieve past messages with a profile.

    Args:
        session: Browser session.
        target_urn: Target profile URN.
        mailbox_urn: Authenticated user's profile URN.

    Returns a list of {sender, text, timestamp} dicts, or None if no conversation exists.
    """
    session.ensure_browser()
    api = PlaywrightLinkedinAPI(session=session)
    limit = max(limit, 1)

    conversation_urn = find_conversation_urn(api, target_urn, mailbox_urn)
    if not conversation_urn:
        logger.debug("Not in recent conversations, trying navigation fallback")
        conversation_urn = find_conversation_urn_via_navigation(session, target_urn)
    if not conversation_urn:
        logger.info("No conversation found for %s", target_urn)
        return None

    raw_elements = []
    for start in range(0, max(limit, 1), 50):
        raw = fetch_messages(api, conversation_urn, count=min(50, limit - start), start=start)
        batch = raw.get("data", {}).get("messengerMessagesBySyncToken", {}).get("elements", [])
        if not batch:
            break
        raw_elements.extend(batch)
        if len(raw_elements) >= limit:
            break
    return parse_messages({"data": {"messengerMessagesBySyncToken": {"elements": raw_elements[:limit]}}})


def get_conversation_by_thread_id(session, thread_id: str, mailbox_urn: str, *, limit: int = 50) -> list[dict]:
    """Retrieve past messages by the thread_id returned from personal inbox."""
    session.ensure_browser()
    api = PlaywrightLinkedinAPI(session=session)
    limit = max(limit, 1)
    conversation_urn = conversation_urn_from_thread_id(thread_id, mailbox_urn)

    raw_elements = []
    for start in range(0, limit, 50):
        raw = fetch_messages(api, conversation_urn, count=min(50, limit - start), start=start)
        batch = raw.get("data", {}).get("messengerMessagesBySyncToken", {}).get("elements", [])
        if not batch:
            break
        raw_elements.extend(batch)
        if len(raw_elements) >= limit:
            break
    return parse_messages({"data": {"messengerMessagesBySyncToken": {"elements": raw_elements[:limit]}}})
