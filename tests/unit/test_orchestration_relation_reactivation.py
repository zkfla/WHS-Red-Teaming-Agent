"""H2: relation-only changes drive role reactivation, noise does not.

Every case here changes *no node identity and no node content*. The only
thing that moves is an edge, so a fingerprint built from nodes alone cannot
tell any of these apart -- which is exactly the gap this closes.
"""

from __future__ import annotations

from redteam.attack_graph.identity import (
    endpoint_node_id,
    host_node_id,
    make_node_id,
    principal_node_id,
)
from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.core.enums import GraphEdgeType, GraphNodeType
from redteam.core.ids import AgentId
from redteam.orchestration.agents.definition import builtin_agent_definitions
from redteam.orchestration.agents.registry import default_registry

HOST = "a.local"
PATH = "/orders/1"
RESOURCE = make_node_id(GraphNodeType.RESOURCE, "orders/1")


async def base_graph() -> AttackGraphService:
    """A graph whose nodes Web/API already knows about, and no relations."""

    service = AttackGraphService(InMemoryAttackGraphStore())
    await service.upsert_node(
        GraphNodeType.HOST, node_id=host_node_id(HOST), attrs={"in_scope": True}
    )
    await service.upsert_node(
        GraphNodeType.ENDPOINT,
        node_id=endpoint_node_id("GET", PATH, host=HOST),
        attrs={"web_inspected": True, "status_code": 200},
        facts={"host": HOST, "path": PATH, "method": "GET"},
    )
    await service.upsert_node(
        GraphNodeType.PRINCIPAL,
        node_id=principal_node_id("session:alice"),
        attrs={"authenticated": True},
    )
    await service.upsert_node(
        GraphNodeType.RESOURCE,
        node_id=RESOURCE,
        attrs={"api_resource_template": True},
    )
    return service


async def web_fingerprint(service: AttackGraphService) -> str:
    return default_registry().revision_fingerprint(
        AgentId("web-api-agent"), await service.observe(step=1)
    )


def web_profile():
    return next(
        item
        for item in builtin_agent_definitions()
        if str(item.agent_id) == "web-api-agent"
    ).context_profile


# -- profile wiring --------------------------------------------------------


def test_the_web_profile_subscribes_to_the_access_relations_it_plans_from() -> None:
    profile = web_profile()

    assert {
        GraphEdgeType.CAN_ACCESS,
        GraphEdgeType.OWNS,
        GraphEdgeType.AUTHENTICATES_TO,
        GraphEdgeType.OBSERVED,
        GraphEdgeType.EXPOSES,
    } <= set(profile.include_edge_types)
    assert GraphNodeType.RESOURCE in profile.include_node_types


def test_a_role_that_subscribes_to_nothing_is_not_woken_by_relations() -> None:
    recon = next(
        item
        for item in builtin_agent_definitions()
        if str(item.agent_id) == "recon-agent"
    )

    assert recon.context_profile.include_edge_types == []


# -- observation contents --------------------------------------------------


async def test_the_observation_carries_edge_counts_and_relation_digests() -> None:
    service = await base_graph()
    await service.link(
        GraphEdgeType.CAN_ACCESS,
        principal_node_id("session:alice"),
        RESOURCE,
        {"outcome": "session_access_observed"},
    )

    observation = await service.observe(step=1)

    assert observation.edge_counts["can_access"] == 1
    assert observation.relation_digests["can_access"]


# -- positive: a relevant relation wakes the role --------------------------


async def test_an_access_relation_appearing_changes_the_web_fingerprint() -> None:
    service = await base_graph()
    before = await web_fingerprint(service)

    await service.link(
        GraphEdgeType.CAN_ACCESS,
        principal_node_id("session:alice"),
        RESOURCE,
        {"outcome": "session_access_observed"},
    )
    after = await web_fingerprint(service)

    assert before != after


async def test_an_authentication_relation_appearing_changes_the_web_fingerprint() -> None:
    service = await base_graph()
    before = await web_fingerprint(service)

    await service.link(
        GraphEdgeType.AUTHENTICATES_TO,
        principal_node_id("session:alice"),
        endpoint_node_id("GET", PATH, host=HOST),
        {"scheme": "session"},
    )
    after = await web_fingerprint(service)

    assert before != after


async def test_a_changed_relation_property_changes_the_web_fingerprint() -> None:
    service = await base_graph()
    await service.link(
        GraphEdgeType.CAN_ACCESS,
        principal_node_id("session:alice"),
        RESOURCE,
        {"outcome": "session_access_observed"},
    )
    before = await web_fingerprint(service)

    # Same edge, same endpoints, different access conclusion.
    await service.link(
        GraphEdgeType.CAN_ACCESS,
        principal_node_id("session:alice"),
        RESOURCE,
        {"outcome": "access_control_violation"},
    )
    after = await web_fingerprint(service)

    assert before != after


# -- negative: noise must not wake the role --------------------------------


async def test_an_unsubscribed_relation_type_does_not_change_the_web_fingerprint() -> None:
    service = await base_graph()
    before = await web_fingerprint(service)

    await service.link(
        GraphEdgeType.RELATED_TO,
        principal_node_id("session:alice"),
        RESOURCE,
        {"relation": "unrelated"},
    )
    after = await web_fingerprint(service)

    assert before == after


async def test_a_volatile_relation_property_does_not_change_the_web_fingerprint() -> None:
    service = await base_graph()
    await service.link(
        GraphEdgeType.CAN_ACCESS,
        principal_node_id("session:alice"),
        RESOURCE,
        {"outcome": "session_access_observed"},
    )
    before = await web_fingerprint(service)

    # Re-materialise the same relation with bookkeeping attached. Provenance,
    # step counters and evidence ids change on every turn by construction.
    await service.link(
        GraphEdgeType.CAN_ACCESS,
        principal_node_id("session:alice"),
        RESOURCE,
        {
            "outcome": "session_access_observed",
            "step": 42,
            "provenance": {"source": "tool:web.http_request", "run_id": "run_x"},
            "evidence_ids": ["ev_later"],
        },
    )
    after = await web_fingerprint(service)

    assert before == after


async def test_relations_do_not_wake_a_role_that_does_not_subscribe() -> None:
    service = await base_graph()
    registry = default_registry()
    before = registry.revision_fingerprint(
        AgentId("recon-agent"), await service.observe(step=1)
    )

    await service.link(
        GraphEdgeType.CAN_ACCESS,
        principal_node_id("session:alice"),
        RESOURCE,
        {"outcome": "session_access_observed"},
    )
    after = registry.revision_fingerprint(
        AgentId("recon-agent"), await service.observe(step=1)
    )

    assert before == after


async def test_an_unrelated_node_still_does_not_change_the_web_fingerprint() -> None:
    # Regression guard for the pre-existing behaviour: adding a node type the
    # role does not include must stay invisible even now that relations are
    # part of the fingerprint. ASSET is the type outside the web/api profile;
    # an open HYPOTHESIS would legitimately count, which is why it is not used
    # as the "unrelated" example here.
    service = await base_graph()
    before = await web_fingerprint(service)

    await service.upsert_node(
        GraphNodeType.ASSET,
        key="com.example.app",
        attrs={"present": True},
    )
    after = await web_fingerprint(service)

    assert before == after
