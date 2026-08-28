import asyncio
from pathlib import Path

import pytest

from whs_redteam.audit import AuditLogger
from whs_redteam.config import EngagementConfig
from whs_redteam.exceptions import RequestLimitExceeded
from whs_redteam.inventory import InventoryStore
from whs_redteam.policy.budget import RequestBudget
from whs_redteam.policy.scope import ScopeGuard
from whs_redteam.tools.browser import WebExplorer
from whs_redteam.tools.network_gate import NetworkGate


def make_explorer(tmp_path: Path) -> tuple[WebExplorer, InventoryStore, NetworkGate]:
    config = EngagementConfig(
        name="test",
        targets=("https://example.test",),
        inventory_db_path=tmp_path / "inventory.db",
        audit_log_path=tmp_path / "events.jsonl",
        screenshots_dir=tmp_path / "screenshots",
    )
    logger = AuditLogger(config.audit_log_path)
    gate = NetworkGate(ScopeGuard(config, logger), RequestBudget(1, 10, 60), logger)
    store = InventoryStore(config.inventory_db_path)
    return WebExplorer(config, store, gate, logger, run_id="test-run"), store, gate


def test_risky_navigation_is_deferred(tmp_path: Path) -> None:
    explorer, store, _ = make_explorer(tmp_path)
    try:
        assert explorer._risky_navigation_reason("https://example.test/delete/1", "Delete")
        assert explorer._risky_navigation_reason("https://example.test/profile", "Sign out")
        assert (
            explorer._risky_navigation_reason("https://example.test/products", "Products")
            is None
        )
    finally:
        store.close()


def test_network_gate_remains_halted_after_fatal_budget_error(tmp_path: Path) -> None:
    _, store, gate = make_explorer(tmp_path)

    async def scenario() -> None:
        await gate.authorize("https://example.test/first", "GET")
        with pytest.raises(RequestLimitExceeded):
            await gate.authorize("https://example.test/second", "GET")
        with pytest.raises(RequestLimitExceeded):
            gate.raise_if_halted()

    try:
        asyncio.run(scenario())
    finally:
        store.close()
