"""Scope-controlled browser confirmation for a harmless XSS execution canary."""

from __future__ import annotations

from urllib.parse import urlencode, urlsplit, urlunsplit

from playwright.async_api import Request, Route, WebSocketRoute, async_playwright

from whs_redteam.audit import AuditLogger
from whs_redteam.config import EngagementConfig, InjectionFixture
from whs_redteam.exceptions import PolicyViolation
from whs_redteam.policy.dns import resolve_and_pin_targets
from whs_redteam.tools.network_gate import NetworkGate


class BrowserXSSConfirmer:
    """Execute only a DOM-attribute canary; never read cookies or page content."""

    def __init__(
        self,
        config: EngagementConfig,
        network_gate: NetworkGate,
        audit_logger: AuditLogger,
    ) -> None:
        self.config = config
        self.network_gate = network_gate
        self.audit_logger = audit_logger

    async def confirm(
        self, fixture: InjectionFixture, token: str
    ) -> dict[str, object]:
        if fixture.actor_id is not None:
            return {
                "status": "unsupported_actor_context",
                "confirmed": False,
            }
        payload = (
            '<img src="data:image/gif;base64,invalid" '
            "onerror=\"document.documentElement.dataset.whsXss='"
            + token
            + "'\">"
        )
        parsed = urlsplit(fixture.target_url)
        target_url = urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path or "/",
                urlencode({fixture.parameter: payload}),
                "",
            )
        )
        try:
            pin_set = resolve_and_pin_targets(self.config)
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(
                    headless=True,
                    args=pin_set.chromium_args(),
                )
                try:
                    context = await browser.new_context(
                        ignore_https_errors=False,
                        service_workers="block",
                    )
                    try:
                        async def scoped_route(route: Route, request: Request) -> None:
                            await self._route_request(
                                route,
                                request,
                                fixture.approval_scope_url,
                            )

                        await context.route("**/*", scoped_route)
                        await context.route_web_socket(
                            "**/*", self._block_websocket
                        )
                        page = await context.new_page()
                        await page.goto(
                            target_url,
                            wait_until="domcontentloaded",
                            timeout=15_000,
                        )
                        try:
                            await page.wait_for_function(
                                "token => document.documentElement.dataset.whsXss === token",
                                token,
                                timeout=2_000,
                            )
                            confirmed = True
                        except Exception:
                            confirmed = False
                    finally:
                        await context.close()
                finally:
                    await browser.close()
        except Exception as exc:
            self.audit_logger.emit(
                "xss_browser_confirmation_unavailable",
                fixture_id=fixture.fixture_id,
                error_type=type(exc).__name__,
            )
            return {
                "status": "unavailable",
                "confirmed": False,
                "error_type": type(exc).__name__,
            }
        return {
            "status": "confirmed" if confirmed else "not_executed",
            "confirmed": confirmed,
        }

    async def _route_request(
        self,
        route: Route,
        request: Request,
        approval_scope_url: str,
    ) -> None:
        scheme = urlsplit(request.url).scheme.lower()
        if scheme in {"about", "blob", "data"}:
            await route.continue_()
            return
        if scheme not in {"http", "https"}:
            await route.abort("blockedbyclient")
            return
        if not self._within_scope(request.url, approval_scope_url):
            await route.abort("blockedbyclient")
            return
        try:
            await self.network_gate.authorize(request.url, request.method)
        except PolicyViolation:
            await route.abort("blockedbyclient")
            return
        await route.continue_()

    async def _block_websocket(self, websocket: WebSocketRoute) -> None:
        await websocket.close(code=1008, reason="Blocked by engagement policy")

    @staticmethod
    def _within_scope(candidate_url: str, scope_url: str) -> bool:
        candidate = urlsplit(candidate_url)
        scope = urlsplit(scope_url)
        candidate_port = candidate.port or (
            443 if candidate.scheme == "https" else 80
        )
        scope_port = scope.port or (443 if scope.scheme == "https" else 80)
        scope_path = (scope.path or "/").rstrip("/") + "/"
        candidate_path = (candidate.path or "/").rstrip("/") + "/"
        return (
            candidate.scheme.lower(),
            (candidate.hostname or "").lower(),
            candidate_port,
        ) == (
            scope.scheme.lower(),
            (scope.hostname or "").lower(),
            scope_port,
        ) and candidate_path.startswith(scope_path)
