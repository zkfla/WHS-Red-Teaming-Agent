"""Cards, details and native bindings share the same metadata role view."""

from dataclasses import replace

import pytest

from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.specialists import SpecialistRunPolicy
from redteam.core.enums import ToolHealthStatus
from redteam.orchestration.agents.tool_access import ready_role_ids, specialist_tool_ids
from redteam.orchestration.engine import InvestigationEngine
from redteam.orchestration.local_tools import LocalToolContext
from redteam.orchestration.seed import seed_from_engagement
from redteam.orchestration.state import RuntimeState
from redteam.orchestration.tool_discovery import (
    build_native_bindings,
    tool_get_detail_handler,
    tool_list_cards_handler,
)
from tests.fakes import DummyToolGateway
from tests.integration.test_native_react_loop import ScriptedNativeLLM, _engagement, _native_deps
from tests.unit.test_tool_discovery import _inventory_descriptor, _mcp_tool


async def prepared(*, enabled=True, unhealthy=False):
    engagement = _engagement()
    deps = _native_deps(engagement, ScriptedNativeLLM([]))
    web = _mcp_tool("web.observe", "observe", capability="web", operations=["response_analysis"])
    if unhealthy:
        web = web.model_copy(update={"health_status": ToolHealthStatus.UNHEALTHY})
    gateway = DummyToolGateway(tools=[_inventory_descriptor(), web])
    deps = replace(deps, tool_catalog=gateway, tool_gateway=gateway,
                   specialist_policy=SpecialistRunPolicy(
                       fingerprint="a" * 64, max_instances=2, max_activations=4,
                       max_tasks_per_strategy=4, max_strategy_revisions=2,
                   ) if enabled else None)
    await seed_from_engagement(AttackGraphService(deps.graph_store), engagement)
    runtime = RuntimeState(run_id="run-views", engagement_id=engagement.engagement_id,
                           current_goal="Observe authorized host", current_agent="web-api-agent")
    return deps, runtime


async def test_specialist_card_detail_and_native_scope_match():
    deps, runtime = await prepared()
    ctx = LocalToolContext(deps=deps, runtime=runtime)
    cards = await tool_list_cards_handler({"kind": "mcp_tool"}, ctx)
    assert cards.ok
    assert {item["tool_id"] for item in cards.payload["cards"]} == {"web.observe"}
    assert (await tool_get_detail_handler({"tool_id": "web.observe"}, ctx)).ok
    assert not (await tool_get_detail_handler({"tool_id": "recon.inventory"}, ctx)).ok
    context = await InvestigationEngine(deps).context(runtime, None)
    visible = {identity for row in context["tools_by_role"].values() for identity in row}
    assert visible == {"web.observe"}
    bindings = build_native_bindings(catalog_tool_ids=visible, local_tool_ids=[])
    assert set(bindings.values()) == visible
    assert await specialist_tool_ids(deps, runtime) == visible
    unknown = runtime.model_copy(update={"current_agent": "unregistered"})
    assert await specialist_tool_ids(deps, unknown) == set()


async def test_legacy_keeps_union_and_unhealthy_role_cannot_activate():
    deps, runtime = await prepared(enabled=False)
    assert await specialist_tool_ids(deps, runtime) is None
    cards = await tool_list_cards_handler(
        {"kind": "mcp_tool"}, LocalToolContext(deps=deps, runtime=runtime),
    )
    assert {item["tool_id"] for item in cards.payload["cards"]} == {
        "web.observe", "recon.inventory",
    }
    deps, runtime = await prepared(unhealthy=True)
    assert await specialist_tool_ids(deps, runtime) == set()
    assert "web-api-agent" not in await ready_role_ids(deps)
    # Troubleshooting visibility is not permission/readiness.
    assert await specialist_tool_ids(deps, runtime, include_unhealthy=True) == {"web.observe"}


@pytest.mark.parametrize("role_id", [None, "unregistered"])
async def test_missing_role_never_falls_back_to_catalog_union(role_id):
    deps, runtime = await prepared()
    runtime = runtime.model_copy(update={"current_agent": role_id})
    cards = await tool_list_cards_handler(
        {"kind": "mcp_tool"}, LocalToolContext(deps=deps, runtime=runtime),
    )
    assert cards.ok and cards.payload["cards"] == []
