import asyncio
from pathlib import Path

import httpx

from whs_redteam.audit import AuditLogger
from whs_redteam.config import EngagementConfig
from whs_redteam.inventory import InventoryStore
from whs_redteam.policy.budget import RequestBudget
from whs_redteam.policy.scope import ScopeGuard
from whs_redteam.tools.http_explorer import HttpExplorer
from whs_redteam.tools.network_gate import NetworkGate


def test_http_explorer_crawls_get_links_without_persisting_query_values(
    tmp_path: Path,
) -> None:
    config = EngagementConfig(
        name="http-explorer-test",
        targets=("https://example.test",),
        discovery_mode="http",
        follow_discovered_links=True,
        max_depth=2,
        max_pages=5,
        requests_per_second=20,
        inventory_db_path=tmp_path / "inventory.db",
        audit_log_path=tmp_path / "events.jsonl",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text='<a href="/next?q=secret-value">next</a>',
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<p>done</p>",
        )

    logger = AuditLogger(config.audit_log_path)
    store = InventoryStore(config.inventory_db_path)
    gate = NetworkGate(
        ScopeGuard(config, logger),
        RequestBudget.from_config(config, logger),
        logger,
    )
    try:
        summary = asyncio.run(
            HttpExplorer(
                config,
                store,
                gate,
                logger,
                run_id="http-run",
                transport=httpx.MockTransport(handler),
                resolver=lambda _hostname: ("8.8.8.8",),
            ).explore()
        )
        schemas = store.list_api_request_schemas("http-run")
    finally:
        store.close()

    assert summary.pages_visited == 2
    assert summary.endpoints_discovered == 2
    assert any("q" in schema.query_schema for schema in schemas)
    persisted = config.inventory_db_path.read_bytes() + config.audit_log_path.read_bytes()
    assert b"secret-value" not in persisted
