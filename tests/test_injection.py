import asyncio
from pathlib import Path

import httpx

from whs_redteam.api_schema import infer_query_schema
from whs_redteam.audit import AuditLogger
from whs_redteam.config import EngagementConfig, InjectionFixture
from whs_redteam.injection import (
    ReflectedXSSProbe,
    SQLInjectionProbe,
    discover_injection_fixtures,
)
from whs_redteam.inventory import InventoryStore
from whs_redteam.models import (
    ActionProposal,
    ApiRequestSchemaRecord,
    Capability,
    ExploitabilityVerdict,
)
from whs_redteam.orchestration.adapters import ProbeContext
from whs_redteam.orchestration.orchestrator import AgentOrchestrator
from whs_redteam.policy.budget import RequestBudget
from whs_redteam.policy.scope import ScopeGuard
from whs_redteam.tools.actor_sessions import ActorSessionPool
from whs_redteam.tools.http_client import SafeHttpClient
from whs_redteam.tools.network_gate import NetworkGate


def _config(tmp_path: Path) -> EngagementConfig:
    return EngagementConfig(
        name="injection-test",
        targets=("https://example.test",),
        inventory_db_path=tmp_path / "inventory.db",
        audit_log_path=tmp_path / "events.jsonl",
        requests_per_second=20,
        max_requests=100,
        validation_repetitions=2,
        injection_fixtures=(
            InjectionFixture(
                fixture_id="sql-item-id",
                approval_scope_url="https://example.test",
                target_url="https://example.test/sql",
                parameter="id",
                baseline_value="1",
                parameter_kind="integer",
                checks=frozenset({"sqli"}),
            ),
            InjectionFixture(
                fixture_id="reflected-search",
                approval_scope_url="https://example.test",
                target_url="https://example.test/reflect",
                parameter="q",
                baseline_value="safe",
                checks=frozenset({"xss_reflection"}),
            ),
        ),
        enabled_capabilities=frozenset(
            {
                "discovery",
                "api_schema",
                "auth_session",
                "resource_model",
                "finding_validation",
                "sqli_safe",
                "xss_safe",
            }
        ),
        approval_hmac_secret_env="TEST_APPROVAL_SECRET",
    )


def _transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        value = request.url.params.get("id") or request.url.params.get("q") or ""
        if request.url.path == "/sql":
            if value.endswith("'") and "AND" not in value:
                return httpx.Response(
                    500,
                    headers={"content-type": "text/plain"},
                    text="sqlite3.OperationalError: unrecognized token",
                )
            body = "record" if value in {"1", "1 AND 1=1"} else "empty"
            return httpx.Response(
                200, headers={"content-type": "text/plain"}, text=body
            )
        if request.url.path == "/reflect":
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text=f"<html><body>{value}</body></html>",
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


class _ConfirmedBrowserCanary:
    async def confirm(
        self, fixture: InjectionFixture, token: str
    ) -> dict[str, object]:
        assert fixture.fixture_id == "reflected-search"
        assert len(token) == 20
        return {"status": "confirmed", "confirmed": True}


def test_query_schema_discovery_creates_bounded_value_free_candidates(
    tmp_path: Path,
) -> None:
    config = EngagementConfig(
        name="auto-injection-test",
        targets=("https://example.test",),
        inventory_db_path=tmp_path / "inventory.db",
        audit_log_path=tmp_path / "events.jsonl",
        auto_injection_discovery=True,
        enabled_capabilities=frozenset({"sqli_safe", "xss_safe"}),
        approval_hmac_secret_env="TEST_APPROVAL_SECRET",
    )
    schemas = (
        ApiRequestSchemaRecord(
            endpoint_url="https://example.test/sql",
            method="GET",
            resource_type="document",
            query_schema=infer_query_schema("https://example.test/sql?id=94821"),
        ),
        ApiRequestSchemaRecord(
            endpoint_url="https://example.test/reflect",
            method="GET",
            resource_type="document",
            query_schema=infer_query_schema(
                "https://example.test/reflect?q=private-search&token=secret-value"
            ),
        ),
        ApiRequestSchemaRecord(
            endpoint_url="https://example.test/delete",
            method="GET",
            resource_type="document",
            query_schema=infer_query_schema("https://example.test/delete?id=7"),
        ),
        ApiRequestSchemaRecord(
            endpoint_url="https://example.test/post-only",
            method="POST",
            resource_type="fetch",
            query_schema=infer_query_schema("https://example.test/post-only?q=value"),
        ),
    )

    fixtures = discover_injection_fixtures(config, schemas)

    assert {(item.target_url, item.parameter) for item in fixtures} == {
        ("https://example.test/sql", "id"),
        ("https://example.test/reflect", "q"),
    }
    by_parameter = {item.parameter: item for item in fixtures}
    assert by_parameter["id"].checks == frozenset({"sqli"})
    assert by_parameter["id"].baseline_value == "1"
    assert by_parameter["q"].checks == frozenset({"sqli", "xss_reflection"})
    assert by_parameter["q"].baseline_value == "safe"
    assert all(item.source == "discovered_query_schema" for item in fixtures)
    serialized = " ".join(item.model_dump_json() for item in fixtures)
    assert "94821" not in serialized
    assert "private-search" not in serialized
    assert "secret-value" not in serialized


def test_bounded_sqli_and_reflected_xss_probes_create_findings(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    logger = AuditLogger(config.audit_log_path)
    store = InventoryStore(config.inventory_db_path)
    budget = RequestBudget.from_config(config, logger)
    gate = NetworkGate(ScopeGuard(config, logger), budget, logger)
    transport = _transport()
    anonymous = SafeHttpClient(gate, logger, transport=transport)
    sessions = ActorSessionPool(config, gate, logger, transport=transport)
    validation_id = "validation-test"
    arguments = (
        config.injection_fixtures,
        anonymous,
        sessions,
        lambda: validation_id,
        config.validation_repetitions,
    )
    context = ProbeContext(run_id="agent-run", inventory=store, audit_logger=logger)

    async def execute() -> tuple[dict[str, object], dict[str, object]]:
        sql = await SQLInjectionProbe(*arguments).execute(
            ActionProposal(
                capability=Capability.SQLI_SAFE,
                target_url="https://example.test",
                fixture_id="sql-item-id",
                reason="test bounded SQL injection fixture",
            ),
            context,
        )
        xss = await ReflectedXSSProbe(*arguments).execute(
            ActionProposal(
                capability=Capability.XSS_SAFE,
                target_url="https://example.test",
                fixture_id="reflected-search",
                reason="test bounded reflected XSS fixture",
            ),
            context,
        )
        return sql, xss

    try:
        sql_output, xss_output = asyncio.run(execute())
        findings = store.list_findings(validation_id)
        decision = AgentOrchestrator(config, store, logger)._decide_exploitability(
            validation_id
        )
    finally:
        store.close()

    assert sql_output["confirmed"] is True
    assert set(sql_output["confirmed_modes"]) == {"error_signal", "boolean_signal"}
    assert xss_output["confirmed_html_injection"] is True
    assert xss_output["javascript_execution_attempted"] is False
    assert {finding["category"] for finding in findings} == {
        "sqli",
        "xss_reflection",
    }
    assert decision.verdict is ExploitabilityVerdict.CONFIRMED
    assert decision.bounded_exploit_demonstrated is True


def test_reflected_xss_alone_is_reported_as_potential(tmp_path: Path) -> None:
    config = _config(tmp_path)
    logger = AuditLogger(config.audit_log_path)
    store = InventoryStore(config.inventory_db_path)
    try:
        # Reuse the probe path, then remove SQL from the fixture set by only executing XSS.
        budget = RequestBudget.from_config(config, logger)
        gate = NetworkGate(ScopeGuard(config, logger), budget, logger)
        transport = _transport()
        probe = ReflectedXSSProbe(
            config.injection_fixtures,
            SafeHttpClient(gate, logger, transport=transport),
            ActorSessionPool(config, gate, logger, transport=transport),
            lambda: "xss-only",
            2,
        )
        asyncio.run(
            probe.execute(
                ActionProposal(
                    capability=Capability.XSS_SAFE,
                    target_url="https://example.test",
                    fixture_id="reflected-search",
                    reason="test bounded reflected XSS fixture",
                ),
                ProbeContext(run_id="agent-run", inventory=store, audit_logger=logger),
            )
        )
        decision = AgentOrchestrator(config, store, logger)._decide_exploitability(
            "xss-only"
        )
    finally:
        store.close()

    assert decision.verdict is ExploitabilityVerdict.POTENTIAL
    assert decision.bounded_exploit_demonstrated is False


def test_browser_confirmed_xss_is_a_bounded_confirmed_exploit(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    logger = AuditLogger(config.audit_log_path)
    store = InventoryStore(config.inventory_db_path)
    try:
        budget = RequestBudget.from_config(config, logger)
        gate = NetworkGate(ScopeGuard(config, logger), budget, logger)
        transport = _transport()
        probe = ReflectedXSSProbe(
            config.injection_fixtures,
            SafeHttpClient(gate, logger, transport=transport),
            ActorSessionPool(config, gate, logger, transport=transport),
            lambda: "xss-execution",
            2,
            _ConfirmedBrowserCanary(),
        )
        output = asyncio.run(
            probe.execute(
                ActionProposal(
                    capability=Capability.XSS_SAFE,
                    target_url="https://example.test",
                    fixture_id="reflected-search",
                    reason="test browser-confirmed reflected XSS fixture",
                ),
                ProbeContext(run_id="agent-run", inventory=store, audit_logger=logger),
            )
        )
        findings = store.list_findings("xss-execution")
        decision = AgentOrchestrator(config, store, logger)._decide_exploitability(
            "xss-execution"
        )
    finally:
        store.close()

    assert output["javascript_execution_attempted"] is True
    assert output["javascript_execution_confirmed"] is True
    assert {finding["category"] for finding in findings} == {
        "xss_reflection",
        "xss",
    }
    assert decision.verdict is ExploitabilityVerdict.CONFIRMED
    assert decision.bounded_exploit_demonstrated is True
