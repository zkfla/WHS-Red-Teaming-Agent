import asyncio
from pathlib import Path

import pytest

from whs_redteam.audit import AuditLogger
from whs_redteam.config import EngagementConfig
from whs_redteam.exceptions import ScopeViolation
from whs_redteam.policy.budget import RequestBudget
from whs_redteam.policy.scope import ScopeGuard
from whs_redteam.tools.browser import normalize_url
from whs_redteam.tools.network_gate import NetworkGate


def test_normalize_url_removes_fragment_and_default_port_without_reordering_query() -> None:
    assert (
        normalize_url("HTTPS://Example.Test:443/path?z=2&a=1#section")
        == "https://example.test/path?z=2&a=1"
    )


def test_normalize_url_resolves_relative_links() -> None:
    assert (
        normalize_url("../api/items", "https://example.test/app/page")
        == "https://example.test/api/items"
    )


def test_network_gate_applies_scope_before_budget(tmp_path: Path) -> None:
    config = EngagementConfig(name="test", targets=("https://example.test",))
    logger = AuditLogger(tmp_path / "events.jsonl")
    budget = RequestBudget(10, 10, 60)
    gate = NetworkGate(ScopeGuard(config, logger), budget, logger)

    with pytest.raises(ScopeViolation):
        asyncio.run(gate.authorize("https://outside.test", "GET"))

    assert budget.request_count == 0
    assert gate.observed == 1
    assert gate.blocked == 1


def test_network_gate_consumes_budget_for_allowed_request() -> None:
    config = EngagementConfig(name="test", targets=("https://example.test",))
    budget = RequestBudget(10, 10, 60)
    gate = NetworkGate(ScopeGuard(config), budget)

    asyncio.run(gate.authorize("https://example.test/api/items", "GET"))

    assert budget.request_count == 1
    assert gate.observed == 1
    assert gate.blocked == 0
