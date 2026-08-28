import asyncio
from pathlib import Path

import httpx

from whs_redteam.audit import AuditLogger
from whs_redteam.authorization import AuthorizationMatrixProbe
from whs_redteam.config import (
    ActorConfig,
    AuthorizationFixture,
    EngagementConfig,
)
from whs_redteam.inventory import InventoryStore
from whs_redteam.models import (
    ActionProposal,
    Capability,
    FindingStatus,
    RiskLevel,
)
from whs_redteam.orchestration.adapters import ProbeContext
from whs_redteam.policy.budget import RequestBudget
from whs_redteam.policy.scope import ScopeGuard
from whs_redteam.tools.actor_sessions import ActorSessionPool
from whs_redteam.tools.network_gate import NetworkGate


def _config(tmp_path: Path) -> EngagementConfig:
    return EngagementConfig(
        name="authorization-fixture",
        targets=("https://example.test",),
        inventory_db_path=tmp_path / "inventory.db",
        audit_log_path=tmp_path / "events.jsonl",
        actors=(
            ActorConfig(
                actor_id="owner",
                header_env={"Authorization": "OWNER_AUTH"},
            ),
            ActorConfig(
                actor_id="other",
                header_env={"Authorization": "OTHER_AUTH"},
            ),
        ),
        authorization_fixtures=(
            AuthorizationFixture(
                fixture_id="order-idor",
                approval_scope_url="https://example.test/api",
                target_url="https://example.test/api/orders/123",
                negative_control_url="https://example.test/api/controls/denied",
                owner_actor="owner",
                non_owner_actors=("other",),
            ),
        ),
        enabled_capabilities=frozenset(
            {
                "discovery",
                "api_schema",
                "auth_session",
                "resource_model",
                "finding_validation",
                "idor_safe",
            }
        ),
        approval_hmac_secret_env="APPROVAL_SECRET",
    )


def _execute(
    tmp_path: Path,
    handler,
) -> tuple[dict[str, object], list[dict[str, object]], bytes]:
    config = _config(tmp_path)
    logger = AuditLogger(config.audit_log_path)
    store = InventoryStore(config.inventory_db_path)
    gate = NetworkGate(
        ScopeGuard(config, logger),
        RequestBudget(30, 30, 60),
        logger,
    )
    sessions = ActorSessionPool(
        config,
        gate,
        logger,
        transport=httpx.MockTransport(handler),
        environment={
            "OWNER_AUTH": "Bearer owner-token",
            "OTHER_AUTH": "Bearer other-token",
        },
    )
    probe = AuthorizationMatrixProbe(
        config.authorization_fixtures,
        sessions,
        lambda: "validation-1",
        repetitions=2,
    )
    proposal = ActionProposal(
        capability=Capability.IDOR_SAFE,
        target_url=config.authorization_fixtures[0].approval_scope_url,
        method="GET",
        risk_level=RiskLevel.SAFE_ACTIVE,
        reason="approved fixture comparison",
        actor_id="owner",
        fixture_id="order-idor",
    )
    try:
        output = asyncio.run(
            probe.execute(
                proposal,
                ProbeContext("run-1", store, logger),
            )
        )
        findings = store.list_findings("validation-1")
    finally:
        store.close()
    persisted = config.inventory_db_path.read_bytes() + config.audit_log_path.read_bytes()
    return output, findings, persisted


def test_owner_equivalent_non_owner_response_confirms_idor(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        actor = request.headers.get("authorization")
        if request.url.path.endswith("/controls/denied"):
            return httpx.Response(403, text="denied")
        if actor in {"Bearer owner-token", "Bearer other-token"}:
            return httpx.Response(
                200,
                json={"order_id": 123, "private_note": "do-not-persist"},
            )
        return httpx.Response(401)

    output, findings, persisted = _execute(tmp_path, handler)

    assert output["actors_confirmed"] == ["other"]
    assert len(findings) == 1
    assert findings[0]["status"] == FindingStatus.CONFIRMED.value
    assert findings[0]["category"] == "idor"
    assert findings[0]["target_url"].endswith("/orders/{resource_id}")
    assert b"do-not-persist" not in persisted
    assert b"owner-token" not in persisted
    assert b"other-token" not in persisted


def test_denied_non_owner_response_does_not_create_finding(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/controls/denied"):
            return httpx.Response(403)
        if request.headers.get("authorization") == "Bearer owner-token":
            return httpx.Response(200, json={"order_id": 123})
        return httpx.Response(403)

    output, findings, _ = _execute(tmp_path, handler)

    assert output["actors_confirmed"] == []
    assert output["actors_inconclusive"] == []
    assert findings == []


def test_failed_negative_control_prevents_idor_confirmation(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"order_id": 123})

    output, findings, _ = _execute(tmp_path, handler)

    assert output["actors_confirmed"] == []
    assert output["actors_inconclusive"] == ["other"]
    assert findings == []
