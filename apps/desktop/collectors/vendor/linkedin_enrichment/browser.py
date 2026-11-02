"""Persistent Playwright context and LinkedIn access-safety checks."""

import json
import os
import re
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import (
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)
from playwright.async_api import TimeoutError as PlaywrightTimeoutError


class AccessSignal(StrEnum):
    AUTH = "auth"
    CHECKPOINT = "checkpoint"
    RATE_LIMIT = "rate_limit"
    SELECTOR_DRIFT = "selector_drift"


class AccessBlocked(RuntimeError):
    def __init__(self, signal: AccessSignal, message: str):
        super().__init__(message)
        self.signal = signal


class PageUnavailable(RuntimeError):
    """The browser did not expose a usable DOM after navigation."""


def classify_access(
    *,
    url: str,
    status: int | None = None,
    text: str = "",
    selector_ok: bool = True,
) -> AccessSignal | None:
    """Classify access failures from network, URL, page text, and sentinels."""

    lower_url = url.lower()
    lower_text = text.lower()
    if status in {401, 403} or any(
        marker in lower_url for marker in ("/login", "/authwall", "/uas/login")
    ):
        return AccessSignal.AUTH
    if any(
        marker in lower_url
        for marker in ("/checkpoint", "/challenge", "consumer-email-challenge")
    ) or any(marker in lower_text for marker in ("security verification", "captcha")):
        return AccessSignal.CHECKPOINT
    rate_notice = re.compile(
        r"(?:please\s+)?(?:slow down|try again later)[.!]?"
        r"|too many requests(?:[.!]\s*(?:please\s+)?try again later[.!]?)?[.!]?"
        r"|(?:your\s+)?rate limit(?: exceeded| reached)?[.!]?"
        r"|you(?: have|\x27ve) (?:made|sent) too many requests[.!]?(?:\s*please try again later[.!]?)?",
        re.IGNORECASE,
    )
    if status == 429 or any(
        rate_notice.fullmatch(line.strip()) for line in text.splitlines()
    ):
        return AccessSignal.RATE_LIMIT
    if not selector_ok:
        return AccessSignal.SELECTOR_DRIFT
    return None


async def assert_access(
    page: Page,
    *,
    response_status: int | None = None,
    expected_selector: str | None = None,
) -> None:
    captcha_frames = page.locator(
        'iframe[title*="captcha" i], iframe[src*="captcha" i]'
    )
    for index in range(await captcha_frames.count()):
        if await captcha_frames.nth(index).is_visible():
            raise AccessBlocked(
                AccessSignal.CHECKPOINT,
                "LinkedIn access safety signal: checkpoint",
            )
    try:
        body = (await page.locator("body").inner_text(timeout=15_000)) or ""
    except PlaywrightTimeoutError as error:
        raise PageUnavailable(
            f"LinkedIn page body did not become available: {page.url}",
        ) from error
    selector_ok = (
        expected_selector is None or await page.locator(expected_selector).count() > 0
    )
    signal = classify_access(
        url=page.url,
        status=response_status,
        text=body,
        selector_ok=selector_ok,
    )
    if signal:
        raise AccessBlocked(signal, f"LinkedIn access safety signal: {signal.value}")


class PersistentChromium:
    """A user-data-dir context; authentication is never injected as a cookie."""

    def __init__(
        self,
        user_data_dir: Path,
        *,
        headless: bool = True,
        slow_mo: int = 0,
        launch_options: dict[str, Any] | None = None,
    ):
        self.user_data_dir = user_data_dir
        self.headless = headless
        self.slow_mo = slow_mo
        self.launch_options = launch_options or {}
        executable_path = os.environ.get("LINKEDIN_CHROMIUM_EXECUTABLE")
        if executable_path:
            executable = Path(executable_path).expanduser()
            if not executable.is_file():
                raise FileNotFoundError(executable)
            self.launch_options.setdefault("executable_path", str(executable))
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    async def __aenter__(self) -> "PersistentChromium":
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.user_data_dir, 0o700)
        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            str(self.user_data_dir),
            headless=self.headless,
            slow_mo=self.slow_mo,
            timeout=600_000,
            viewport={"width": 1440, "height": 1000},
            **self.launch_options,
        )
        self._context.set_default_timeout(120_000)
        self._context.set_default_navigation_timeout(180_000)
        self._page = (
            self._context.pages[0]
            if self._context.pages
            else (await self._context.new_page())
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._context:
            await self._context.close()
        if self._playwright:
            await self._playwright.stop()
        self._page = None
        self._context = None
        self._playwright = None

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("persistent browser is not running")
        return self._page

    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise RuntimeError("persistent browser is not running")
        return self._context

    async def export_storage_state(self, path: Path) -> None:
        """Export a cross-platform Playwright session to a private file."""

        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(mode=0o600, exist_ok=True)
        os.chmod(path, 0o600)
        await self.context.storage_state(path=path)
        os.chmod(path, 0o600)

    async def import_storage_state(self, path: Path) -> None:
        """Seed this persistent Linux profile from a Playwright storage-state file."""

        state = json.loads(path.read_text(encoding="utf-8"))
        cookies = state.get("cookies")
        origins = state.get("origins")
        if not isinstance(cookies, list) or not isinstance(origins, list):
            raise ValueError("storage state must contain cookies and origins lists")

        await self.context.add_cookies(cookies)
        for origin_state in origins:
            if not isinstance(origin_state, dict):
                raise ValueError("storage-state origins must be objects")
            origin = origin_state.get("origin")
            local_storage = origin_state.get("localStorage")
            if not isinstance(origin, str) or not isinstance(local_storage, list):
                raise ValueError("storage-state origin is malformed")
            parsed = urlparse(origin)
            if parsed.scheme != "https" or not (
                parsed.hostname == "linkedin.com"
                or (parsed.hostname or "").endswith(".linkedin.com")
            ):
                continue
            await self.page.goto(
                origin,
                wait_until="commit",
                timeout=180_000,
            )
            await self.page.evaluate(
                """entries => {
                    for (const entry of entries) {
                        localStorage.setItem(entry.name, entry.value);
                    }
                }""",
                local_storage,
            )
