import asyncio
from pathlib import Path

import httpx
import pytest

from whs_redteam.audit import AuditLogger
from whs_redteam.config import EngagementConfig
from whs_redteam.inventory import InventoryStore
from whs_redteam.models import FindingStatus, PlanItem
from whs_redteam.models import TestCategory as Category
from whs_redteam.models import TestPlan as Plan
from whs_redteam.planning import RulesPlanner
from whs_redteam.policy.scope import ScopeGuard
from whs_redteam.validation.validator import FindingValidator, negative_control_passes


def make_validator(
    tmp_path: Path,
    handler,
) -> tuple[EngagementConfig, InventoryStore, FindingValidator]:
    config = EngagementConfig(
        name="validation-test",
        targets=("https://example.test",),
        max_requests=50,
        requests_per_second=20,
        inventory_db_path=tmp_path / "inventory.db",
        audit_log_path=tmp_path / "events.jsonl",
    )
    store = InventoryStore(config.inventory_db_path)
    logger = AuditLogger(config.audit_log_path)
    validator = FindingValidator(
        config,
        store,
        ScopeGuard(config, logger),
        logger,
        transport=httpx.MockTransport(handler),
        resolver=lambda _: ("93.184.216.34",),
    )
    return config, store, validator


def test_all_catalog_negative_controls_pass() -> None:
    for category in Category:
        assert negative_control_passes(category.value)


def test_stable_failure_is_confirmed_and_deduplicated(tmp_path: Path) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, text="ok")

    config, store, validator = make_validator(tmp_path, handler)
    plan = RulesPlanner(config).plan(config.targets)
    try:
        first_summary, first_findings = asyncio.run(validator.validate(plan))
        second_summary, second_findings = asyncio.run(validator.validate(plan))
        stored = store.list_findings()
    finally:
        store.close()

    assert first_summary.confirmed == 1
    assert second_summary.confirmed == 1
    assert len(first_findings) == 1
    assert len(second_findings) == 1
    assert first_findings[0].status is FindingStatus.CONFIRMED
    assert first_findings[0].reproduction_count == 2
    assert first_findings[0].negative_control_passed is True
    assert len(stored) == 1
    assert len(stored[0]["experiment_ids"]) == 4


def test_non_reproducible_failure_is_inconclusive(tmp_path: Path) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        headers = {"content-type": "text/html"}
        if calls > 1:
            headers.update(
                {
                    "content-security-policy": "default-src 'self'; frame-ancestors 'none'",
                    "x-content-type-options": "nosniff",
                    "referrer-policy": "no-referrer",
                    "strict-transport-security": "max-age=31536000",
                }
            )
        return httpx.Response(200, headers=headers, text="ok")

    _, store, validator = make_validator(tmp_path, handler)
    plan = Plan(
        provider="rules",
        items=(
            PlanItem(
                target_url="https://example.test",
                category=Category.SECURITY_HEADERS,
                reason="reproduction test",
            ),
        ),
    )
    try:
        summary, findings = asyncio.run(validator.validate(plan))
    finally:
        store.close()

    assert summary.confirmed == 0
    assert summary.inconclusive == 1
    assert findings[0].status is FindingStatus.INCONCLUSIVE
    assert findings[0].reproduction_count == 1


def test_failed_negative_control_blocks_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, text="ok")

    monkeypatch.setattr(
        "whs_redteam.validation.validator.negative_control_passes",
        lambda _: False,
    )
    _, store, validator = make_validator(tmp_path, handler)
    plan = Plan(
        provider="rules",
        items=(
            PlanItem(
                target_url="https://example.test",
                category=Category.SECURITY_HEADERS,
                reason="negative-control gate test",
            ),
        ),
    )
    try:
        summary, findings = asyncio.run(validator.validate(plan))
    finally:
        store.close()

    assert summary.confirmed == 0
    assert summary.inconclusive == 1
    assert findings[0].status is FindingStatus.INCONCLUSIVE
    assert findings[0].reproduction_count == 2
    assert findings[0].negative_control_passed is False
