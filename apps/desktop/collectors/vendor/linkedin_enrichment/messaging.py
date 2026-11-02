"""Read-only incremental capture of LinkedIn's active conversation previews."""

import asyncio
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from .browser import PersistentChromium, assert_access
from .storage import Store

MESSAGING_URL = "https://www.linkedin.com/messaging/"
MESSAGES_QUERY_ID = "messengerMessages.5846eeb71c981f11e0134cb6626cc314"
CONVERSATIONS_QUERY_ID = (
    "messengerConversations.9501074288a12f3ae9e3c7ea243bccbf"
)


def _text(value: object) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        text = value.get("text")
        return text.strip() if isinstance(text, str) and text.strip() else None
    return None


def _timestamp(milliseconds: object) -> str | None:
    if not isinstance(milliseconds, (int, float)):
        return None
    return datetime.fromtimestamp(milliseconds / 1000, UTC).isoformat()


def _root_with_elements(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data", {})
    if not isinstance(data, dict):
        return {}
    return next(
        (
            value
            for value in data.values()
            if isinstance(value, dict) and isinstance(value.get("elements"), list)
        ),
        {},
    )


def parse_conversation(
    row: dict[str, Any],
    *,
    self_urn: str | None,
) -> dict[str, Any] | None:
    conversation_urn = row.get("entityUrn") or row.get("backendUrn")
    if not isinstance(conversation_urn, str):
        return None
    participants = [
        participant.get("hostIdentityUrn")
        for participant in row.get("conversationParticipants", [])
        if isinstance(participant, dict)
        and isinstance(participant.get("hostIdentityUrn"), str)
    ]
    messages = row.get("messages", {})
    message_rows = (
        messages.get("elements", []) if isinstance(messages, dict) else []
    )
    latest = (
        message_rows[0]
        if message_rows and isinstance(message_rows[0], dict)
        else {}
    )
    sender = latest.get("sender", {}) if isinstance(latest, dict) else {}
    sender_urn = (
        sender.get("hostIdentityUrn") if isinstance(sender, dict) else None
    )
    message_urn = latest.get("entityUrn") if isinstance(latest, dict) else None
    body = latest.get("body", {}) if isinstance(latest, dict) else {}
    parsed_message = (
        {
            "message_urn": message_urn,
            "sender_urn": sender_urn,
            "delivered_at": _timestamp(latest.get("deliveredAt")),
            "subject": _text(latest.get("subject")),
            "body": _text(body),
            "incoming": bool(sender_urn and self_urn and sender_urn != self_urn),
        }
        if isinstance(message_urn, str)
        else None
    )
    return {
        "conversation_urn": conversation_urn,
        "backend_urn": row.get("backendUrn"),
        "title": _text(row.get("title")),
        "participants": participants,
        "created_at": _timestamp(row.get("createdAt")),
        "last_activity_at": _timestamp(row.get("lastActivityAt")),
        "last_read_at": _timestamp(row.get("lastReadAt")),
        "unread_count": int(row.get("unreadCount") or 0),
        "read": bool(row.get("read")),
        "message": parsed_message,
        "messages": [parsed_message] if parsed_message is not None else [],
    }


def parse_message(
    row: dict[str, Any],
    *,
    self_urn: str | None,
) -> dict[str, Any] | None:
    message_urn = row.get("entityUrn") or row.get("backendUrn")
    if not isinstance(message_urn, str):
        return None
    sender = row.get("sender", {})
    sender_urn = (
        sender.get("hostIdentityUrn") if isinstance(sender, dict) else None
    )
    return {
        "message_urn": message_urn,
        "sender_urn": sender_urn,
        "delivered_at": _timestamp(row.get("deliveredAt")),
        "subject": _text(row.get("subject")),
        "body": _text(row.get("body")),
        "incoming": bool(sender_urn and self_urn and sender_urn != self_urn),
    }


async def _conversation_titles(page: Any) -> list[str | None]:
    texts = await page.locator(".msg-conversation-listitem").all_inner_texts()
    return [
        next(
            (
                line.strip()
                for line in text.splitlines()
                if line.strip()
            ),
            None,
        )
        for text in texts
    ]


async def scan_messages(
    store: Store,
    user_data_dir: Path,
    *,
    headless: bool = True,
) -> dict[str, int]:
    async with PersistentChromium(user_data_dir, headless=headless) as browser:
        page = browser.page
        captured: asyncio.Future[tuple[str | None, list[dict[str, Any]]]] = (
            asyncio.get_running_loop().create_future()
        )

        async def capture(response: Any) -> None:
            if "messengerConversations" not in response.url or captured.done():
                return
            payload = await response.json()
            root = _root_with_elements(payload)
            elements = root.get("elements", []) if isinstance(root, dict) else []
            mailbox = re.search(r"mailboxUrn:([^,)]+)", unquote(response.url))
            self_urn = mailbox.group(1) if mailbox else None
            captured.set_result((self_urn, elements))

        page.on("response", capture)
        await page.goto(MESSAGING_URL, wait_until="domcontentloaded", timeout=180_000)
        await page.wait_for_selector("main", timeout=30_000)
        await assert_access(page, expected_selector="main")
        self_urn, rows = await asyncio.wait_for(captured, timeout=30)
        parsed = [
            conversation
            for row in rows
            if isinstance(row, dict)
            and (conversation := parse_conversation(row, self_urn=self_urn))
            is not None
        ]
        for conversation, title in zip(
            parsed,
            await _conversation_titles(page),
            strict=False,
        ):
            conversation["title"] = conversation["title"] or title
        store.store_conversations(parsed)
        return {
            "conversations_checked": len(parsed),
            "messages_observed": sum(item["message"] is not None for item in parsed),
            "unread_conversations": sum(item["unread_count"] > 0 for item in parsed),
        }


async def backfill_messages(
    store: Store,
    user_data_dir: Path,
    *,
    headless: bool = True,
    max_conversations: int = 500,
) -> dict[str, int]:
    async with PersistentChromium(user_data_dir, headless=headless) as browser:
        page = await browser.context.new_page()
        response_rows: list[dict[str, Any]] = []
        self_urn: str | None = None
        response_tasks: set[asyncio.Task[None]] = set()

        async def capture(response: Any) -> None:
            nonlocal self_urn
            if "messengerConversations" not in response.url:
                return
            payload = await response.json()
            root = payload.get("data", {}).get(
                "messengerConversationsBySyncToken",
                {},
            )
            if isinstance(root, dict):
                response_rows.extend(
                    row
                    for row in root.get("elements", [])
                    if isinstance(row, dict)
                )
            mailbox = re.search(r"mailboxUrn:([^,)]+)", unquote(response.url))
            if mailbox:
                self_urn = mailbox.group(1)

        def schedule_capture(response: Any) -> None:
            if "messengerConversations" not in response.url:
                return
            task = asyncio.create_task(capture(response))
            response_tasks.add(task)
            task.add_done_callback(response_tasks.discard)

        page.on("response", schedule_capture)
        await page.goto(MESSAGING_URL, wait_until="domcontentloaded", timeout=180_000)
        await page.wait_for_selector("main", timeout=30_000)
        await assert_access(page, expected_selector="main")
        await page.wait_for_timeout(5_000)

        stable_rounds = 0
        previous = 0
        for _ in range(30):
            items = page.locator(".msg-conversation-listitem")
            count = await items.count()
            if count >= max_conversations or stable_rounds >= 3:
                break
            if count:
                await items.last.scroll_into_view_if_needed()
            await page.mouse.wheel(0, 1_000)
            await page.wait_for_timeout(2_000)
            stable_rounds = stable_rounds + 1 if count == previous else 0
            previous = count

        while response_tasks:
            await asyncio.gather(*response_tasks)
        cookies = {
            cookie["name"]: cookie["value"]
            for cookie in await browser.context.cookies("https://www.linkedin.com")
        }
        csrf_token = cookies.get("JSESSIONID", "").strip('"')
        if not csrf_token or not self_urn:
            raise RuntimeError("LinkedIn messaging credentials are unavailable")

        paginated_rows: list[dict[str, Any]] = []
        next_cursor: str | None = None
        for _ in range(30):
            mailbox = quote(self_urn, safe="")
            cursor = (
                f"nextCursor:{quote(next_cursor, safe='')}"
                if next_cursor
                else f"lastUpdatedBefore:{int(time.time() * 1000)}"
            )
            variables = (
                "(query:(predicateUnions:List("
                "(conversationCategoryPredicate:(category:PRIMARY_INBOX)))),"
                f"count:20,mailboxUrn:{mailbox},{cursor})"
            )
            url = (
                "https://www.linkedin.com/voyager/api/"
                "voyagerMessagingGraphQL/graphql?"
                f"queryId={CONVERSATIONS_QUERY_ID}&variables={variables}"
            )
            result = await page.evaluate(
                """async ({url, csrfToken}) => {
                  const response = await fetch(url, {
                    credentials: 'include',
                    headers: {
                      'csrf-token': csrfToken,
                      'x-restli-protocol-version': '2.0.0'
                    }
                  });
                  return {status: response.status, body: await response.text()};
                }""",
                {"url": url, "csrfToken": csrf_token},
            )
            if result["status"] != 200:
                raise RuntimeError(
                    "LinkedIn conversations request returned "
                    f"HTTP {result['status']}"
                )
            payload = json.loads(result["body"])
            root = _root_with_elements(payload)
            rows = root.get("elements", []) if isinstance(root, dict) else []
            paginated_rows.extend(row for row in rows if isinstance(row, dict))
            metadata = root.get("metadata", {}) if isinstance(root, dict) else {}
            next_cursor = (
                metadata.get("nextCursor")
                if isinstance(metadata, dict)
                else None
            )
            if (
                not rows
                or not isinstance(next_cursor, str)
                or len(paginated_rows) >= max_conversations
            ):
                break
            await page.wait_for_timeout(250)
        if paginated_rows:
            response_rows = paginated_rows

        conversations = {
            parsed["conversation_urn"]: parsed
            for row in response_rows
            if (
                parsed := parse_conversation(
                    row,
                    self_urn=self_urn,
                )
            )
            is not None
        }
        selected = list(conversations.values())[:max_conversations]
        for conversation, title in zip(
            selected,
            await _conversation_titles(page),
            strict=False,
        ):
            conversation["title"] = conversation["title"] or title
        message_count = 0
        message_rows_received = 0
        for index, conversation in enumerate(selected):
            urn = quote(conversation["conversation_urn"], safe="")
            url = (
                "https://www.linkedin.com/voyager/api/"
                f"voyagerMessagingGraphQL/graphql?queryId={MESSAGES_QUERY_ID}"
                f"&variables=(conversationUrn:{urn})"
            )
            result = await page.evaluate(
                """async ({url, csrfToken}) => {
                  const response = await fetch(url, {
                    credentials: 'include',
                    headers: {
                      'csrf-token': csrfToken,
                      'x-restli-protocol-version': '2.0.0'
                    }
                  });
                  return {status: response.status, body: await response.text()};
                }""",
                {"url": url, "csrfToken": csrf_token},
            )
            if result["status"] != 200:
                raise RuntimeError(
                    f"LinkedIn messages request returned HTTP {result['status']}"
                )
            payload = json.loads(result["body"])
            root = _root_with_elements(payload)
            rows = root.get("elements", []) if isinstance(root, dict) else []
            message_rows_received += len(rows)
            conversation["messages"] = [
                message
                for row in rows
                if isinstance(row, dict)
                and (
                    message := parse_message(
                        row,
                        self_urn=self_urn,
                    )
                )
                is not None
            ]
            message_count += len(conversation["messages"])
            if index % 10 == 9:
                store.store_conversations(selected[index - 9 : index + 1])
            await page.wait_for_timeout(250)
        store.store_conversations(selected)
        return {
            "rows_received": len(response_rows),
            "conversations": len(selected),
            "message_rows_received": message_rows_received,
            "messages": message_count,
            "unread_conversations": sum(
                item["unread_count"] > 0 for item in selected
            ),
        }
