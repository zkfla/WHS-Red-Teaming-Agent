import asyncio
from pathlib import Path

import httpx

from whs_redteam.audit import AuditLogger
from whs_redteam.config import ActorConfig, EngagementConfig
from whs_redteam.inventory import InventoryStore
from whs_redteam.policy.budget import RequestBudget
from whs_redteam.policy.scope import ScopeGuard
from whs_redteam.tests_catalog.runner import AssessmentRunner
from whs_redteam.tools.network_gate import NetworkGate


def test_runner_creates_structured_observations(tmp_path: Path, monkeypatch) -> None:
    target = "https://example.test"
    config = EngagementConfig(
        name="test",
        targets=(target,),
        inventory_db_path=tmp_path / "inventory.db",
        audit_log_path=tmp_path / "events.jsonl",
        actors=(
            ActorConfig(
                actor_id="user_a",
                header_env={"Authorization": "WHS_TEST_USER_A_AUTH"},
            ),
        ),
    )
    monkeypatch.setenv("WHS_TEST_USER_A_AUTH", "Bearer secret-actor-token")
    logger = AuditLogger(config.audit_log_path)
    gate = NetworkGate(ScopeGuard(config, logger), RequestBudget(20, 20, 60), logger)

    def handler(request: httpx.Request) -> httpx.Response:
        headers = [
            ("content-type", "text/html; charset=utf-8"),
            ("content-security-policy", "default-src 'self'; frame-ancestors 'none'"),
            ("x-content-type-options", "nosniff"),
            ("referrer-policy", "no-referrer"),
            ("strict-transport-security", "max-age=31536000"),
            ("set-cookie", "session=secret-value; Path=/"),
        ]
        if request.headers.get("origin"):
            headers.extend(
                [
                    ("access-control-allow-origin", request.headers["origin"]),
                    ("access-control-allow-credentials", "true"),
                ]
            )
        body = (
            b"authenticated actor content"
            if request.headers.get("authorization")
            else b"Traceback (most recent call last): test-only"
        )
        return httpx.Response(
            200,
            headers=headers,
            content=body,
        )

    with InventoryStore(config.inventory_db_path) as store:
        runner = AssessmentRunner(
            config,
            store,
            gate,
            logger,
            transport=httpx.MockTransport(handler),
            resolver=lambda _: ("93.184.216.34",),
            run_id="run-day3",
        )
        summary = asyncio.run(runner.run((target,)))
        observations = store.list_observations("run-day3")

    assert summary.targets_assessed == 1
    assert summary.observations == 6
    assert summary.failed == 3
    assert summary.passed == 2
    assert summary.informational == 1
    assert len(observations) == 6
    assert "secret-value" not in str(observations)
    assert "secret-actor-token" not in str(observations)
    assert all(item["experiment_id"] for item in observations)
    assert all(len(str(item["evidence_hash"])) == 64 for item in observations)
    assert all(item["catalog_version"] == "day3-v2" for item in observations)


def test_individual_analyzer_failure_becomes_inconclusive(
    tmp_path: Path, monkeypatch
) -> None:
    target = "https://example.test"
    config = EngagementConfig(
        name="test",
        targets=(target,),
        inventory_db_path=tmp_path / "inventory.db",
        audit_log_path=tmp_path / "events.jsonl",
    )
    logger = AuditLogger(config.audit_log_path)
    gate = NetworkGate(ScopeGuard(config, logger), RequestBudget(20, 20, 60), logger)
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, headers={"content-type": "text/html"}, text="ok")
    )

    def broken_analyzer(_):
        raise LookupError("test analyzer failure")

    monkeypatch.setattr(
        "whs_redteam.tests_catalog.runner.security_headers", broken_analyzer
    )
    with InventoryStore(config.inventory_db_path) as store:
        summary = asyncio.run(
            AssessmentRunner(
                config,
                store,
                gate,
                logger,
                transport=transport,
                resolver=lambda _: ("93.184.216.34",),
                run_id="isolated-run",
            ).run((target,))
        )
        observations = store.list_observations("isolated-run")
    header_result = next(item for item in observations if item["test_id"] == "WEB-HEADERS-001")
    assert header_result["verdict"] == "INCONCLUSIVE"
    assert summary.observations == 5
