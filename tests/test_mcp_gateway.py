import asyncio
from pathlib import Path

import pytest

from whs_redteam.audit import AuditLogger
from whs_redteam.config import EngagementConfig
from whs_redteam.exceptions import CapabilityPolicyViolation
from whs_redteam.inventory import InventoryStore
from whs_redteam.mcp_servers import (
    MCPActionEnvelope,
    MCPServerGuard,
    create_browser_mcp_server,
)
from whs_redteam.models import ActionProposal, Capability, RiskLevel
from whs_redteam.orchestration.adapters import (
    MCPProbeAdapter,
    MCPToolBinding,
    ProbeRegistry,
)
from whs_redteam.orchestration.dispatcher import ActionDispatcher
from whs_redteam.orchestration.mcp_transport import OfficialMCPTransport
from whs_redteam.policy.capabilities import CapabilityPolicy
from whs_redteam.policy.scope import ScopeGuard

pytest.importorskip("mcp")


def config(tmp_path: Path, capabilities=frozenset({"discovery"})):
    return EngagementConfig(
        name="mcp-gateway-test",
        targets=("https://example.test",),
        enabled_capabilities=capabilities,
        inventory_db_path=tmp_path / "inventory.db",
        audit_log_path=tmp_path / "events.jsonl",
    )


def test_dispatcher_to_real_mcp_server_revalidates_policy(tmp_path: Path) -> None:
    engagement = config(tmp_path)
    logger = AuditLogger(engagement.audit_log_path)
    guard = MCPServerGuard(engagement, logger)
    calls: list[str] = []

    async def handler(request, approved):
        calls.append(approved.target_url)
        return {"pages": 2, "server_run_id": request.run_id}

    server = create_browser_mcp_server(guard, handler)
    transport = OfficialMCPTransport({"browser": server})
    adapter = MCPProbeAdapter(
        (MCPToolBinding(Capability.DISCOVERY, "browser", "discover"),),
        transport,
    )
    store = InventoryStore(engagement.inventory_db_path)
    dispatcher = ActionDispatcher(
        CapabilityPolicy(engagement, ScopeGuard(engagement, logger)),
        ProbeRegistry(),
        store,
        logger,
        tool_adapter=adapter,
    )
    try:
        result = asyncio.run(
            dispatcher.dispatch(
                ActionProposal(
                    capability=Capability.DISCOVERY,
                    target_url="https://example.test/catalog",
                    risk_level=RiskLevel.PASSIVE,
                    reason="bounded MCP discovery",
                ),
                run_id="run-mcp",
            )
        )
    finally:
        store.close()

    assert calls == ["https://example.test/catalog"]
    assert result.output == {"pages": 2, "server_run_id": "run-mcp"}


def test_server_ignores_untrusted_central_approval_boolean(tmp_path: Path) -> None:
    engagement = config(tmp_path, frozenset({"idor_safe"}))
    guard = MCPServerGuard(engagement, AuditLogger(engagement.audit_log_path))
    request = MCPActionEnvelope(
        call_id="call-1",
        action_id="action-1",
        run_id="run-1",
        engagement_name=engagement.name,
        capability=Capability.IDOR_SAFE,
        target_url="https://example.test/orders/1",
        method="GET",
        risk_level=RiskLevel.SAFE_ACTIVE,
        approval_verified=True,
    )

    with pytest.raises(CapabilityPolicyViolation, match="independently verified"):
        guard.authorize(request, Capability.IDOR_SAFE)


def test_server_rejects_engagement_and_capability_binding_mismatch(
    tmp_path: Path,
) -> None:
    engagement = config(tmp_path)
    guard = MCPServerGuard(engagement, AuditLogger(engagement.audit_log_path))
    request = MCPActionEnvelope(
        call_id="call-2",
        action_id="action-2",
        run_id="run-2",
        engagement_name="different-engagement",
        capability=Capability.DISCOVERY,
        target_url="https://example.test",
        method="GET",
        risk_level=RiskLevel.PASSIVE,
    )

    with pytest.raises(CapabilityPolicyViolation, match="engagement binding"):
        guard.authorize(request, Capability.DISCOVERY)
