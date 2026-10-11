"""Actual assembled catalog gate, generic role IDs, no live device claims."""

from dataclasses import replace

import pytest

from redteam.core.enums import ToolHealthStatus
from redteam.orchestration.agents.base import AgentSpec
from redteam.orchestration.agents.registry import AgentRegistry
from redteam.orchestration.agents.tool_access import (
    ready_role_ids,
    register_role_readiness,
    role_readiness,
    specialist_tool_ids,
)
from redteam.orchestration.local_tools import LocalToolContext
from redteam.orchestration.mobile_mcp_binding import MobileMCPEligibility, VendorPolicy
from tests.fakes import DummyToolGateway
from tests.integration.test_specialist_tool_view import prepared
from tests.unit.test_tool_discovery import _mcp_tool


async def mobile_view(*, healthy=True, approved=False):
    deps, runtime = await prepared()
    agents = AgentRegistry()
    definition = deps.agents.definition("web-api-agent").model_copy(update={
        "agent_id": "arbitrary-device-role", "capabilities": ["mobile"],
        "tool_selection": {"capability_tags": ["mobile"]},
    })
    # Same selector semantics as existing capability-scoped default definitions.
    definition.tool_selection = None
    agents.register_definition(definition)
    agents.register(AgentSpec(agent_id=definition.agent_id, name="Device worker",
                              capabilities=["mobile"]))
    tool = _mcp_tool("mobile.vendor.observe", "observe", capability="mobile")
    tool = tool.model_copy(update={
        "server_id": "operator-selected-server",
        "health_status": ToolHealthStatus.HEALTHY if healthy else ToolHealthStatus.UNHEALTHY,
    })
    gateway = DummyToolGateway(tools=[tool])
    policy = VendorPolicy(vendor_id="custom", managed_server_ids=frozenset({tool.server_id}),
                          fallback_map={}, hash_required_server_ids=frozenset({tool.server_id}),
                          approval_ref="approved-mobile-artifacts")
    engagement = deps.engagement
    if approved:
        engagement = engagement.model_copy(update={
            "scope": engagement.scope.model_copy(update={"allowed_assets": ["test-app"]}),
            "metadata": {"approved-mobile-artifacts": {
                "enabled": True, "assets": {"test-app": "a" * 64},
            }},
        })
    gate = MobileMCPEligibility(gateway, gateway, engagement, vendor_policies=[policy])
    deps = replace(deps, agents=agents, tool_catalog=gate, tool_gateway=gate)
    return deps, runtime.model_copy(update={"current_agent": definition.agent_id}), tool


@pytest.mark.parametrize("approved,healthy,ready", [
    (False, True, False), (True, False, False), (True, True, True),
])
async def test_vendor_gate_same_for_descriptor_projection_readiness_role(approved, healthy, ready):
    deps, runtime, tool = await mobile_view(healthy=healthy, approved=approved)
    rows = await role_readiness(deps)
    assert rows[0]["catalog_ready"] is ready
    assert bool(await ready_role_ids(deps)) is ready
    assert bool(await specialist_tool_ids(deps, runtime)) is ready
    assert bool(deps.tool_catalog.all_descriptors()) is ready
    assert rows[0]["target_execution_verified"] is False  # no app/device was contacted
    assert rows[0]["execution_permission_granted"] is False
    assert tool.health_status == (ToolHealthStatus.HEALTHY if healthy
                                  else ToolHealthStatus.UNHEALTHY)  # raw health never rewritten


async def test_missing_device_tools_report_conditions_without_fake_or_call():
    deps, runtime, _ = await mobile_view()
    register_role_readiness(deps.local_tools)
    result = await deps.local_tools.call("agent.readiness", {}, LocalToolContext(deps, runtime))
    assert result.ok and result.runtime_outcome is None
    row = result.payload["roles"][0]
    assert row["reason_code"] == "no_eligible_tool_or_target"
    assert not row["visible_tool_ids"] and not result.payload["target_execution_verified"]


async def test_unknown_health_visibility_not_claimed_healthy_and_legacy_launch_preserved():
    deps, runtime = await prepared()
    rows = await role_readiness(deps)
    web = next(row for row in rows if row["role_id"] == str(runtime.current_agent))
    assert web["activation_eligible"] and not web["catalog_ready"]
    assert web["reason_code"] == "no_healthy_eligible_tool"
    assert str(runtime.current_agent) in await ready_role_ids(deps)
