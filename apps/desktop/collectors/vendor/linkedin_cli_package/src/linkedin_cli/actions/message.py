# linkedin/actions/message.py
import logging
import time
from typing import Dict, Any

from playwright.sync_api import Error as PlaywrightError, Locator
from linkedin_cli.browser.nav import goto_page, human_type, dump_page_html

logger = logging.getLogger(__name__)

LINKEDIN_MESSAGING_URL = "https://www.linkedin.com/messaging/thread/new/"

# Selector fallback chains: semantic/ARIA first, then class-based.
# LinkedIn A/B tests UI variants per account and renames classes often.
# Each key maps to a list tried in order; first with a match wins.
SELECTOR_CHAINS = {
    # ── New thread: recipient search ──
    "connections_input": [
        'input[role="combobox"][placeholder*="name"]',
        'input[class*="msg-connections"]',
        'input[placeholder*="Type a name"]',
        'input[type="text"][aria-owns]',
    ],
    "search_result_row": [
        'ul[role="listbox"] li[role="option"]',
        'div[class*="msg-connections-typeahead__search-result-row"]',
        'li[class*="search-result"]',
    ],
    # ── Thread: compose area ──
    "compose_input": [
        'div[role="textbox"][aria-label*="Write a message"]',
        'div[role="textbox"][aria-label*="message"i]',
        'div[class*="msg-form__contenteditable"]',
        'div[contenteditable="true"]',
    ],
    "compose_send": [
        'button[type="submit"][class*="msg-form"]',
        'button[class*="send-btn"]',
        'button[class*="send-button"]',
        'form button[type="submit"]',
        'button[type="submit"]',
    ],
    "attach_button": [
        'button[aria-label*="Attach" i]',
        'button[aria-label*="Add attachment" i]',
        'button[title*="Attach" i]',
    ],
}


def _find(page, key: str, timeout: int = 5000) -> Locator:
    """Try each selector in the chain for *key*, return the first with matches.

    Raises PlaywrightError if none match within *timeout* ms.
    """
    chain = SELECTOR_CHAINS[key]
    for sel in chain:
        loc = page.locator(sel)
        try:
            loc.first.wait_for(state="attached", timeout=timeout)
            logger.debug("Selector hit for %s: %s", key, sel)
            return loc
        except (PlaywrightError, TimeoutError):
            continue
    tried = ", ".join(chain)
    raise PlaywrightError(f"No selector matched for '{key}'. Tried: {tried}")


# ── Public entry point ────────────────────────────────────────────


def send_raw_message(session, profile: Dict[str, Any], message: str, *, attachments: list[str] | None = None) -> bool:
    """Send an arbitrary message to a profile. Returns True if sent."""
    public_identifier = profile.get("public_identifier")
    attachments = attachments or []

    if _send_message(session, profile, message, attachments=attachments):
        return True
    dump_page_html(session, profile, category="message_direct")

    if not attachments and _send_message_via_api(session, profile, message):
        return True

    logger.error("All send methods failed for %s", public_identifier)
    return False


def _send_message(session, profile: Dict[str, Any], message: str, *, attachments: list[str] | None = None) -> bool:
    """Navigate to /messaging/thread/new/?recipient=<urn>, compose, send.

    Uses the target URN (promoted to its own Lead column in crm.0005) to
    skip the search-by-name step entirely. Post-migration 0007 the Lead
    row no longer carries first_name/last_name, so name-based search is
    not available anyway.
    """
    from linkedin_cli.api.messaging.utils import encode_urn

    public_identifier = profile.get("public_identifier")
    target_urn = profile.get("urn")
    if not target_urn:
        logger.error("Cannot send via direct thread: no URN for %s", public_identifier)
        return False
    try:
        thread_url = f"{LINKEDIN_MESSAGING_URL}?recipient={encode_urn(target_urn)}"
        goto_page(
            session,
            action=lambda: session.page.goto(thread_url),
            expected_url_pattern="/messaging",
            timeout=30_000,
            error_message="Error opening messaging thread",
        )
        session.wait(1, 2)

        human_type(
            _find(session.page, "compose_input").first,
            message,
            min_delay=10,
            max_delay=50,
        )
        _attach_files(session, attachments or [])
        _find(session.page, "compose_send").first.click(delay=200)
        session.wait(0.5, 1)
        logger.info("Message sent to %s (direct thread)", public_identifier)
        return True
    except (PlaywrightError, TimeoutError) as e:
        logger.error("Failed to send message to %s (direct thread) → %s", public_identifier, e)
        return False


def _attach_files(session, files: list[str]) -> bool:
    if not files:
        return False
    from linkedin_cli.actions.posts import _existing_paths

    paths = _existing_paths(files)
    page = session.page
    try:
        inputs = page.locator('input[type="file"]')
        if inputs.count() > 0:
            inputs.last.set_input_files(paths)
        else:
            with page.expect_file_chooser(timeout=5_000) as chooser_info:
                _find(page, "attach_button", timeout=5_000).first.click(force=True)
            chooser_info.value.set_files(paths)
    except PlaywrightError:
        with page.expect_file_chooser(timeout=5_000) as chooser_info:
            _find(page, "attach_button", timeout=5_000).first.click(force=True)
        chooser_info.value.set_files(paths)

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        send = _find(page, "compose_send", timeout=1_000).first
        if send.is_enabled():
            return True
        session.wait(0.5, 1.0)
    return True


def _send_message_via_api(session, profile: Dict[str, Any], message: str) -> bool:
    """Last-resort fallback: send via Voyager Messaging API.

    Requires profile dict to contain 'urn' (target profile URN).
    """
    from linkedin_cli.api.client import PlaywrightLinkedinAPI
    from linkedin_cli.api.messaging import send_message
    from linkedin_cli.actions.conversations import find_conversation_urn, find_conversation_urn_via_navigation

    public_identifier = profile.get("public_identifier")
    target_urn = profile.get("urn")
    if not target_urn:
        logger.error("API send failed for %s → no URN in profile dict", public_identifier)
        return False

    mailbox_urn = session.self_profile["urn"]
    api = PlaywrightLinkedinAPI(session=session)

    conversation_urn = find_conversation_urn(api, target_urn, mailbox_urn)
    if not conversation_urn:
        conversation_urn = find_conversation_urn_via_navigation(session, target_urn)
    if not conversation_urn:
        logger.error("API send failed for %s → no conversation found", public_identifier)
        return False

    try:
        send_message(api, conversation_urn, message, mailbox_urn)
        logger.info("Message sent to %s (API fallback)", public_identifier)
        return True
    except Exception as e:
        logger.error("API send failed for %s → %s", public_identifier, e)
        return False
