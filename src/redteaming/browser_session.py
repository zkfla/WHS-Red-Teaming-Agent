from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

from .auth import HttpIdentityProfile
from .models import Scope


class BrowserSessionError(RuntimeError):
    pass


class BrowserSessionCapture(Protocol):
    def capture(self, url: str, scope: Scope, identity_name: str) -> HttpIdentityProfile:
        ...


def validate_browser_url(url: str, scope: Scope) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise BrowserSessionError("browser handoff requires an absolute HTTP(S) URL")
    if not scope.allows(url):
        raise BrowserSessionError("browser handoff URL is outside the authorized Scope")


@dataclass(frozen=True)
class PlaywrightSessionCapture:
    """Interactive, operator-driven session handoff; credentials never persist."""

    headless: bool = False
    timeout_ms: int = 30_000

    def capture(self, url: str, scope: Scope, identity_name: str) -> HttpIdentityProfile:
        validate_browser_url(url, scope)
        if not identity_name.strip():
            raise BrowserSessionError("identity name is required")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserSessionError(
                "Playwright is not installed; install the optional browser dependency "
                "and its browser binary first"
            ) from exc
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=self.headless)
                context = browser.new_context()
                page = context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                input("Complete the authorized login in the browser, then press Enter here: ")
                cookies = context.cookies()
                browser.close()
        except Exception as exc:  # provider/browser dependent
            raise BrowserSessionError(f"browser session capture failed: {exc}") from exc
        if not cookies:
            raise BrowserSessionError("no browser cookies were captured")
        cookie_header = "; ".join(f"{item['name']}={item['value']}" for item in cookies)
        return HttpIdentityProfile(identity_name.strip(), (("Cookie", cookie_header),))
