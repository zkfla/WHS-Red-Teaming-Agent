"""Scope-controlled Playwright web explorer."""

from __future__ import annotations

import asyncio
import hashlib
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit
from uuid import uuid4

from playwright.async_api import Page, Request, Response, Route, WebSocketRoute, async_playwright

from whs_redteam.api_schema import (
    canonical_endpoint_url,
    infer_json_schema,
    infer_query_schema,
)
from whs_redteam.audit import AuditLogger
from whs_redteam.config import EngagementConfig
from whs_redteam.exceptions import PolicyViolation, RuntimeLimitExceeded
from whs_redteam.inventory import InventoryStore
from whs_redteam.models import (
    ApiRequestSchemaRecord,
    DeferredLinkRecord,
    EndpointRecord,
    ExplorationSummary,
    FormRecord,
    PageRecord,
)
from whs_redteam.policy.dns import resolve_and_pin_targets
from whs_redteam.sanitizer import sanitize_data, sanitize_url
from whs_redteam.tools.network_gate import NetworkGate


def normalize_url(url: str, base_url: str | None = None) -> str:
    absolute = urljoin(base_url or url, url)
    parsed = urlsplit(absolute)
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower().rstrip(".")
    port = parsed.port
    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    display_host = f"[{hostname}]" if ":" in hostname else hostname
    netloc = display_host if port is None or default_port else f"{display_host}:{port}"
    path = parsed.path or "/"
    return urlunsplit((scheme, netloc, path, parsed.query, ""))


@dataclass(frozen=True, slots=True)
class _QueueItem:
    url: str
    depth: int
    discovered_from: str | None


class WebExplorer:
    def __init__(
        self,
        config: EngagementConfig,
        inventory: InventoryStore,
        network_gate: NetworkGate,
        audit_logger: AuditLogger,
        run_id: str | None = None,
    ) -> None:
        self.config = config
        self.inventory = inventory
        self.network_gate = network_gate
        self.audit_logger = audit_logger
        self.run_id = run_id or uuid4().hex
        self.pages_failed = 0
        self._current_page_url: str | None = None

    async def explore(self) -> ExplorationSummary:
        self.config.screenshots_dir.mkdir(parents=True, exist_ok=True)
        queue = deque(_QueueItem(normalize_url(url), 0, None) for url in self.config.targets)
        queued = {item.url for item in queue}
        visited: set[str] = set()

        pin_set = resolve_and_pin_targets(self.config)
        self.audit_logger.emit("dns_targets_pinned", addresses=pin_set.addresses)
        try:
            async with asyncio.timeout(self.config.max_runtime_minutes * 60):
                async with async_playwright() as playwright:
                    browser = await playwright.chromium.launch(
                        headless=self.config.browser_headless,
                        args=pin_set.chromium_args(),
                    )
                    try:
                        context = await browser.new_context(
                            ignore_https_errors=False,
                            service_workers="block",
                        )
                        try:
                            await context.route("**/*", self._route_request)
                            await context.route_web_socket("**/*", self._block_websocket)
                            page = await context.new_page()
                            context.on(
                                "page",
                                lambda popup: asyncio.create_task(popup.close()),
                            )
                            page.on("response", self._record_response)

                            while queue and len(visited) < self.config.max_pages:
                                self.network_gate.raise_if_halted()
                                item = queue.popleft()
                                if item.url in visited or item.depth > self.config.max_depth:
                                    continue
                                decision = self.network_gate.scope_guard.evaluate(item.url, "GET")
                                if not decision.allowed:
                                    continue
                                visited.add(item.url)
                                links = await self._visit_page(page, item)
                                self.network_gate.raise_if_halted()
                                if (
                                    item.depth >= self.config.max_depth
                                    or not self.config.follow_discovered_links
                                ):
                                    continue
                                for link in links:
                                    if link not in visited and link not in queued:
                                        queued.add(link)
                                        queue.append(
                                            _QueueItem(link, item.depth + 1, item.url)
                                        )
                        finally:
                            await context.close()
                    finally:
                        await browser.close()
        except TimeoutError as exc:
            self.audit_logger.emit("engagement_halted", reason="maximum runtime exceeded")
            raise RuntimeLimitExceeded("maximum engagement runtime exceeded") from exc

        counts = self.inventory.counts(self.run_id)
        summary = ExplorationSummary(
            run_id=self.run_id,
            pages_visited=counts["pages"],
            pages_failed=self.pages_failed,
            forms_discovered=counts["forms"],
            endpoints_discovered=counts["endpoints"],
            links_deferred=counts["deferred_links"],
            requests_observed=self.network_gate.observed,
            requests_blocked=self.network_gate.blocked,
        )
        self.audit_logger.emit("exploration_completed", **summary.model_dump())
        return summary

    async def _route_request(self, route: Route, request: Request) -> None:
        scheme = urlsplit(request.url).scheme.lower()
        if scheme in {"about", "blob", "data"}:
            await route.continue_()
            return
        if scheme not in {"http", "https"}:
            self.audit_logger.emit(
                "network_request_blocked",
                url=request.url,
                method=request.method,
                reason=f"URL scheme {scheme or '[empty]'} is not allowed",
            )
            await route.abort("blockedbyclient")
            return
        try:
            await self.network_gate.authorize(request.url, request.method)
        except PolicyViolation:
            await route.abort("blockedbyclient")
            return
        await route.continue_()

    async def _block_websocket(self, websocket: WebSocketRoute) -> None:
        self.audit_logger.emit(
            "websocket_blocked",
            url=websocket.url,
            reason="WebSockets are disabled in the passive prototype",
        )
        await websocket.close(code=1008, reason="Blocked by engagement policy")

    def _record_response(self, response: Response) -> None:
        request = response.request
        if request.resource_type not in {"xhr", "fetch"}:
            return
        endpoint_url = canonical_endpoint_url(request.url)
        source_page = (
            sanitize_url(
                self._current_page_url, redact_all_query_values=True
            )
            if self._current_page_url
            else None
        )
        self.inventory.save_endpoint(
            self.run_id,
            EndpointRecord(
                url=endpoint_url,
                method=request.method,
                resource_type=request.resource_type,
                source_page=source_page,
                status_code=response.status,
                content_type=response.headers.get("content-type"),
            ),
        )
        post_data = None
        try:
            post_data = request.post_data_json
        except Exception:
            # Non-JSON/binary bodies are intentionally not parsed or persisted.
            pass
        request_content_type = request.headers.get("content-type")
        if request_content_type:
            request_content_type = request_content_type.split(";", 1)[0].strip().lower()
        self.inventory.save_api_request_schema(
            self.run_id,
            ApiRequestSchemaRecord(
                endpoint_url=endpoint_url,
                method=request.method,
                resource_type=request.resource_type,
                source_page=source_page,
                query_schema=infer_query_schema(request.url),
                body_schema=(
                    infer_json_schema(post_data) if post_data is not None else None
                ),
                request_content_type=request_content_type,
            ),
        )

    async def _visit_page(self, page: Page, item: _QueueItem) -> list[str]:
        self._current_page_url = item.url
        status_code: int | None = None
        final_url = item.url
        title = ""
        screenshot_path: str | None = None
        error: str | None = None
        links: list[str] = []
        try:
            response = await page.goto(item.url, wait_until="domcontentloaded", timeout=15_000)
            status_code = response.status if response else None
            final_url = normalize_url(page.url)
            title = await page.title()
            if response is not None:
                endpoint_url = canonical_endpoint_url(final_url)
                content_type = response.headers.get("content-type")
                self.inventory.save_endpoint(
                    self.run_id,
                    EndpointRecord(
                        url=endpoint_url,
                        method="GET",
                        resource_type="document",
                        status_code=status_code,
                        content_type=content_type,
                    ),
                )
                self.inventory.save_api_request_schema(
                    self.run_id,
                    ApiRequestSchemaRecord(
                        endpoint_url=endpoint_url,
                        method="GET",
                        resource_type="document",
                        query_schema=infer_query_schema(final_url),
                    ),
                )
            if self.config.capture_screenshots:
                screenshot_path = await self._take_screenshot(page, final_url)
            await self._record_forms(page, final_url)
            links = await self._extract_links(page, final_url)
        except Exception as exc:  # Playwright exposes multiple navigation failure classes.
            self.pages_failed += 1
            error = str(sanitize_data(f"{type(exc).__name__}: {exc}"[:500]))
            self.audit_logger.emit("page_visit_failed", url=item.url, error=error)
        self.inventory.save_page(
            self.run_id,
            PageRecord(
                url=sanitize_url(item.url, redact_all_query_values=True),
                final_url=sanitize_url(final_url, redact_all_query_values=True),
                depth=item.depth,
                title=title,
                status_code=status_code,
                screenshot_path=screenshot_path,
                discovered_from=(
                    sanitize_url(
                        item.discovered_from, redact_all_query_values=True
                    )
                    if item.discovered_from
                    else None
                ),
                error=error,
            ),
        )
        return links

    async def _extract_links(self, page: Page, page_url: str) -> list[str]:
        anchors = await page.locator("a[href]").evaluate_all(
            """
            elements => elements.map(element => ({
              href: element.getAttribute('href'),
              text: (element.textContent || '').trim()
            }))
            """
        )
        links: set[str] = set()
        for anchor in anchors:
            href = anchor.get("href")
            if not isinstance(href, str) or href.startswith(("javascript:", "mailto:", "tel:")):
                continue
            candidate = normalize_url(href, page_url)
            decision = self.network_gate.scope_guard.evaluate(candidate, "GET")
            if not decision.allowed:
                continue
            reason = self._risky_navigation_reason(candidate, str(anchor.get("text", "")))
            if reason is not None:
                self.inventory.save_deferred_link(
                    self.run_id,
                    DeferredLinkRecord(
                        source_page=sanitize_url(
                            page_url, redact_all_query_values=True
                        ),
                        target_url=sanitize_url(
                            candidate, redact_all_query_values=True
                        ),
                        reason=reason,
                    ),
                )
                continue
            links.add(candidate)
        return sorted(links)

    def _risky_navigation_reason(self, url: str, anchor_text: str) -> str | None:
        value = f"{urlsplit(url).path} {anchor_text}".lower().replace("-", "").replace(" ", "")
        for keyword in self.config.risky_path_keywords:
            normalized_keyword = keyword.lower().replace("-", "").replace(" ", "")
            if normalized_keyword in value:
                return f"navigation contains risky keyword: {keyword}"
        return None

    async def _record_forms(self, page: Page, page_url: str) -> None:
        forms = await page.locator("form").evaluate_all(
            """
            forms => forms.map(form => ({
              action: form.getAttribute('action') || '',
              method: (form.getAttribute('method') || 'GET').toUpperCase(),
              inputs: Array.from(form.querySelectorAll('input, textarea, select')).map(input => ({
                name: input.getAttribute('name') || '',
                type: input.getAttribute('type') || input.tagName.toLowerCase()
              }))
            }))
            """
        )
        for form in forms:
            action_url = normalize_url(form["action"] or page_url, page_url)
            decision = self.network_gate.scope_guard.evaluate(action_url, form["method"])
            self.inventory.save_form(
                self.run_id,
                FormRecord(
                    page_url=sanitize_url(
                        page_url, redact_all_query_values=True
                    ),
                    action_url=sanitize_url(
                        action_url, redact_all_query_values=True
                    ),
                    method=form["method"],
                    input_names=tuple(item["name"] for item in form["inputs"] if item["name"]),
                    input_types=tuple(item["type"] for item in form["inputs"]),
                    in_scope=decision.allowed,
                    policy_reason=decision.reason,
                ),
            )

    async def _take_screenshot(self, page: Page, url: str) -> str:
        digest = hashlib.sha256(url.encode()).hexdigest()[:16]
        path = Path(self.config.screenshots_dir) / f"{digest}.png"
        await page.screenshot(path=str(path), full_page=True)
        return str(path)
