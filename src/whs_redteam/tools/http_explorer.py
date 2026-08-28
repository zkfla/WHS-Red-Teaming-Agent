"""Dependency-light HTTP discovery for environments where Chromium is unavailable."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit
from uuid import uuid4

import httpx

from whs_redteam.api_schema import canonical_endpoint_url, infer_query_schema
from whs_redteam.audit import AuditLogger
from whs_redteam.config import EngagementConfig
from whs_redteam.inventory import InventoryStore
from whs_redteam.models import (
    ApiRequestSchemaRecord,
    EndpointRecord,
    ExplorationSummary,
    PageRecord,
)
from whs_redteam.policy.dns import Resolver, resolve_and_pin_targets, system_resolver
from whs_redteam.sanitizer import sanitize_data, sanitize_url
from whs_redteam.tools.http_client import SafeHttpClient
from whs_redteam.tools.network_gate import NetworkGate


def _normalize(url: str, base: str | None = None) -> str:
    parsed = urlsplit(urljoin(base or url, url))
    return urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", parsed.query, "")
    )


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        values = {name.lower(): value for name, value in attrs}
        href = values.get("href")
        if href:
            self.links.append(href)


@dataclass(frozen=True, slots=True)
class _QueueItem:
    url: str
    depth: int
    discovered_from: str | None = None


class HttpExplorer:
    """GET-only crawler using the same scope, DNS, budget, and audit boundaries."""

    def __init__(
        self,
        config: EngagementConfig,
        inventory: InventoryStore,
        network_gate: NetworkGate,
        audit_logger: AuditLogger,
        run_id: str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver = system_resolver,
    ) -> None:
        self.config = config
        self.inventory = inventory
        self.network_gate = network_gate
        self.audit_logger = audit_logger
        self.run_id = run_id or uuid4().hex
        self.transport = transport
        self.resolver = resolver

    async def explore(self) -> ExplorationSummary:
        initial_dns = resolve_and_pin_targets(self.config, self.resolver)
        client = SafeHttpClient(
            self.network_gate, self.audit_logger, transport=self.transport
        )
        queue = deque(_QueueItem(_normalize(url), 0) for url in self.config.targets)
        queued = {item.url for item in queue}
        visited: set[str] = set()
        pages_failed = 0

        async with asyncio.timeout(self.config.max_runtime_minutes * 60):
            while queue and len(visited) < self.config.max_pages:
                item = queue.popleft()
                if item.url in visited or item.depth > self.config.max_depth:
                    continue
                decision = self.network_gate.scope_guard.evaluate(item.url, "GET")
                if not decision.allowed:
                    continue
                visited.add(item.url)
                try:
                    response = await client.get(item.url)
                except (httpx.HTTPError, UnicodeError, ValueError) as exc:
                    pages_failed += 1
                    self.inventory.save_page(
                        self.run_id,
                        PageRecord(
                            url=sanitize_url(item.url, redact_all_query_values=True),
                            final_url=sanitize_url(item.url, redact_all_query_values=True),
                            depth=item.depth,
                            discovered_from=item.discovered_from,
                            error=str(sanitize_data(f"{type(exc).__name__}: {exc}"))[:500],
                        ),
                    )
                    continue

                final_url = _normalize(response.url)
                content_type = response.headers.get("content-type", "")
                self.inventory.save_page(
                    self.run_id,
                    PageRecord(
                        url=sanitize_url(item.url, redact_all_query_values=True),
                        final_url=sanitize_url(final_url, redact_all_query_values=True),
                        depth=item.depth,
                        status_code=response.status_code,
                        discovered_from=item.discovered_from,
                    ),
                )
                endpoint_url = canonical_endpoint_url(final_url)
                self.inventory.save_endpoint(
                    self.run_id,
                    EndpointRecord(
                        url=endpoint_url,
                        method="GET",
                        resource_type="document",
                        source_page=(
                            sanitize_url(item.discovered_from, redact_all_query_values=True)
                            if item.discovered_from
                            else None
                        ),
                        status_code=response.status_code,
                        content_type=content_type,
                    ),
                )
                self.inventory.save_api_request_schema(
                    self.run_id,
                    ApiRequestSchemaRecord(
                        endpoint_url=endpoint_url,
                        method="GET",
                        resource_type="document",
                        source_page=(
                            sanitize_url(item.discovered_from, redact_all_query_values=True)
                            if item.discovered_from
                            else None
                        ),
                        query_schema=infer_query_schema(final_url),
                    ),
                )
                if (
                    not self.config.follow_discovered_links
                    or item.depth >= self.config.max_depth
                    or "text/html" not in content_type.lower()
                ):
                    continue
                parser = _LinkParser()
                parser.feed(response.text)
                for href in parser.links:
                    if href.startswith(("javascript:", "mailto:", "tel:")):
                        continue
                    candidate = _normalize(href, final_url)
                    if candidate in visited or candidate in queued:
                        continue
                    if not self.network_gate.scope_guard.evaluate(candidate, "GET").allowed:
                        continue
                    queued.add(candidate)
                    queue.append(
                        _QueueItem(
                            candidate,
                            item.depth + 1,
                            sanitize_url(final_url, redact_all_query_values=True),
                        )
                    )

        final_dns = resolve_and_pin_targets(self.config, self.resolver)
        if initial_dns.addresses != final_dns.addresses:
            raise RuntimeError("approved target DNS changed during HTTP discovery")
        counts = self.inventory.counts(self.run_id)
        summary = ExplorationSummary(
            run_id=self.run_id,
            pages_visited=counts["pages"],
            pages_failed=pages_failed,
            forms_discovered=counts["forms"],
            endpoints_discovered=counts["endpoints"],
            links_deferred=counts["deferred_links"],
            requests_observed=self.network_gate.observed,
            requests_blocked=self.network_gate.blocked,
        )
        self.audit_logger.emit("http_exploration_completed", **summary.model_dump())
        return summary
