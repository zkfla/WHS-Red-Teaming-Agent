"""Dynamic candidate generation: meaningful inputs, scoped reactivation.

The point of these tests is that a role is woken by a *changed fact on a node
it already knows* (an endpoint that answered 401 anonymously and now answers
403 for the same session) and is not woken by changes it does not care about.
Node ids and counts are identical in the first case, so an identity-only
fingerprint would silently miss it.
"""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.attack_graph.identity import endpoint_node_id
from redteam.attack_graph.predicates import facts_of
from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.core.contracts.target import EndpointTarget
from redteam.core.contracts.tool import ToolResult
from redteam.core.enums import GraphNodeType
from redteam.core.ids import ActionId, AgentId, InvocationId, ToolId
from redteam.orchestration.agents.builtin import WEB_API_AGENT
from redteam.orchestration.agents.definition import builtin_agent_definitions
from redteam.orchestration.agents.providers import (
    GraphCandidateContext,
    PlaybookCandidateProvider,
    planning_fingerprint,
)
from redteam.orchestration.agents.registry import default_registry
from redteam.orchestration.agents.web import WebCandidateProvider
from redteam.orchestration.authentication import request_key
from redteam.orchestration.observations.mapper import (
    default_result_mapper_registry,
    is_http_status,
)
from redteam.orchestration.observations.web_http import map_web_compare_anonymous

HOST = "a.local"
PATH = "/orders/1"
NOW = datetime.now(timezone.utc)


def http_observation(status: int, *, session_ref: str | None = None, evidence_id="ev_1") -> dict:
    return {
        "kind": "http_response",
        "context_key": request_key("GET", PATH, session_ref),
        "method": "GET",
        "path": PATH,
        "target_ref": HOST,
        "session_ref": session_ref,
        "status_code": status,
        "evidence_id": evidence_id,
    }


async def graph_with(*, observations=(), extra_nodes=()):
    service = AttackGraphService(InMemoryAttackGraphStore())
    await service.upsert_node(GraphNodeType.HOST, key=HOST, attrs={"in_scope": True})
    await service.upsert_node(
        GraphNodeType.ENDPOINT,
        node_id=endpoint_node_id("GET", PATH, host=HOST),
        attrs={"web_inspected": True, "status_code": 200},
        facts={
            "host": HOST,
            "path": PATH,
            "method": "GET",
            "observed_target": PATH,
            "observations": list(observations),
        },
    )
    for node_type, key in extra_nodes:
        await service.upsert_node(node_type, key=key, attrs={"present": True})
    return service


# -- provider context ------------------------------------------------------


def test_can_execute_is_permissive_only_when_the_catalog_could_not_be_checked() -> None:
    context = GraphCandidateContext(
        view=None,  # type: ignore[arg-type]
        observation=None,  # type: ignore[arg-type]
        step=1,
        available_operations=(),
        operations_verified=False,
    )
    assert context.can_execute("web", "auth_attempt")

    checked = GraphCandidateContext(
        view=None,  # type: ignore[arg-type]
        observation=None,  # type: ignore[arg-type]
        step=1,
        available_operations=(("web", "http_request"),),
        operations_verified=True,
    )
    assert checked.can_execute("web", "http_request")
    assert not checked.can_execute("web", "auth_attempt")


# -- reactivation fingerprints --------------------------------------------


async def test_a_changed_fact_on_a_known_node_changes_the_role_fingerprint() -> None:
    registry = default_registry()
    before = await graph_with(observations=[http_observation(401)])
    after = await graph_with(observations=[http_observation(401, session_ref="alice")])

    first = registry.revision_fingerprint(
        AgentId("web-api-agent"), await before.observe(step=1)
    )
    second = registry.revision_fingerprint(
        AgentId("web-api-agent"), await after.observe(step=2)
    )

    assert first != second


async def test_an_unrelated_node_type_does_not_change_the_role_fingerprint() -> None:
    registry = default_registry()
    before = await graph_with(observations=[http_observation(401)])
    after = await graph_with(
        observations=[http_observation(401)],
        # ASSET, not RESOURCE: RESOURCE is deliberately inside the web/api
        # profile now that access relations are part of its fingerprint.
        extra_nodes=[(GraphNodeType.ASSET, "com.example.app")],
    )

    first = registry.revision_fingerprint(
        AgentId("web-api-agent"), await before.observe(step=1)
    )
    second = registry.revision_fingerprint(
        AgentId("web-api-agent"), await after.observe(step=2)
    )

    assert first == second, "ASSET is outside the web role's context profile"


async def test_the_observation_carries_planning_content_digests() -> None:
    service = await graph_with(observations=[http_observation(401)])
    observation = await service.observe(step=1)

    assert observation.content_digests["endpoint"]
    # Identity-only fingerprinting would not see this: the ids are unchanged.
    assert set(observation.node_ids_by_type["endpoint"]) == {
        str(endpoint_node_id("GET", PATH, host=HOST))
    }


async def test_planning_fingerprint_is_independent_of_the_step_counter() -> None:
    service = await graph_with(observations=[http_observation(401)])
    observation = await service.observe(step=1)
    later = await service.observe(step=99)

    assert planning_fingerprint(observation=observation) == planning_fingerprint(
        observation=later
    )


async def test_planning_fingerprint_notices_a_changed_observation() -> None:
    before = await graph_with(observations=[http_observation(401)])
    after = await graph_with(observations=[http_observation(403)])

    assert planning_fingerprint(
        observation=await before.observe(step=1)
    ) != planning_fingerprint(observation=await after.observe(step=1))


# -- drops: a step that matched but cannot be filled ----------------------


async def test_a_step_that_cannot_fill_its_graph_argument_is_reported_as_a_drop() -> None:
    service = AttackGraphService(InMemoryAttackGraphStore())
    await service.upsert_node(GraphNodeType.HOST, key=HOST, attrs={"in_scope": True})
    await service.upsert_node(
        GraphNodeType.SERVICE,
        key=f"{HOST}/http",
        attrs={"responded": True},
        facts={"scheme": "http", "host": HOST},
    )
    context = GraphCandidateContext(
        view=service.view(), observation=await service.observe(step=1), step=1
    )

    batch = await PlaybookCandidateProvider().propose_graph(WEB_API_AGENT, context)

    assert batch.candidates == []
    assert [(item.step_id, item.reason, item.field) for item in batch.drops] == [
        ("inspect-http-service", "graph_prerequisite_missing", "path")
    ]


async def test_web_provider_drops_an_explicitly_unallowlisted_endpoint() -> None:
    service = await graph_with(observations=[http_observation(401)])
    await service.upsert_node(
        GraphNodeType.ENDPOINT,
        node_id=endpoint_node_id("GET", PATH, host=HOST),
        attrs={"path_allowlisted": False},
        facts={"path_allowlisted": False},
    )
    context = GraphCandidateContext(
        view=service.view(), observation=await service.observe(step=1), step=1
    )

    batch = await WebCandidateProvider().propose_graph(WEB_API_AGENT, context)

    assert batch.candidates == []
    assert any(drop.reason == "path_not_allowlisted" for drop in batch.drops)


# -- result mappers --------------------------------------------------------


def comparison_result(**overrides) -> ToolResult:
    output = {
        "target_ref": HOST,
        "anonymous": {
            "status_code": 401, "body_sha256": "a" * 64, "redirect": False,
            "actual_session_ref": None,
        },
        "authenticated": {
            "status_code": 200, "body_sha256": "b" * 64, "redirect": False,
            "actual_session_ref": "alice",
        },
        "same_status": False,
        "same_body": False,
        "verdict": "observation_only",
        "request_count": 2,
    }
    output.update(overrides)
    return ToolResult(
        invocation_id=InvocationId("inv_1"),
        action_id=ActionId("action_1"),
        tool_id=ToolId("web.http_compare_anonymous"),
        success=True,
        output=output,
        started_at=NOW,
        finished_at=NOW,
    )


def test_the_comparison_mapper_is_registered_for_its_own_tool_id() -> None:
    registry = default_result_mapper_registry()

    mutation = registry.map_result(
        tool_id=ToolId("web.http_compare_anonymous"),
        target=EndpointTarget(host=HOST, path=PATH),
        arguments={"target_ref": HOST, "path": PATH, "session_ref": "alice"},
        result=comparison_result(),
    )

    assert registry.last_failure is None
    assert len(mutation.nodes) == 3


def test_the_comparison_mapper_records_what_the_tool_actually_measured() -> None:
    mutation = map_web_compare_anonymous(
        target=EndpointTarget(host=HOST, path=PATH),
        arguments={"target_ref": HOST, "path": PATH, "session_ref": "alice"},
        result=comparison_result(),
    )

    observation = mutation.nodes[0].observations[0]
    assert observation["kind"] == "http_access_comparison"
    assert observation["anonymous_status"] == 401
    assert observation["authenticated_status"] == 200
    assert observation["same_status"] is False
    assert observation["same_body"] is False
    assert observation["session_ref"] == "alice"
    assert observation["anonymous_actual_session_ref"] is None
    assert observation["authenticated_actual_session_ref"] == "alice"
    # The tool is explicit that a status/body difference is not proof of a
    # bypass; the mapper must not upgrade it into one.
    assert observation["verdict"] == "observation_only"
    assert "violation" not in observation
    assert "vulnerable" not in observation
    assert "location" not in observation


def test_anonymous_comparison_without_actual_receipts_preserves_status_without_identity() -> None:
    result = comparison_result(
        anonymous={"status_code": 401, "body_sha256": "a" * 64, "redirect": False},
        authenticated={"status_code": 200, "body_sha256": "b" * 64, "redirect": False},
    )
    mutation = map_web_compare_anonymous(
        target=EndpointTarget(host=HOST, path=PATH),
        arguments={"target_ref": HOST, "path": PATH, "session_ref": "alice"},
        result=result,
    )

    endpoint = next(node for node in mutation.nodes if node.node_type is GraphNodeType.ENDPOINT)
    observation = endpoint.observations[0]
    assert (observation["anonymous_status"], observation["authenticated_status"]) == (401, 200)
    assert observation["session_ref"] == "alice"
    assert "anonymous_actual_session_ref" not in observation
    assert "authenticated_actual_session_ref" not in observation
    assert len(mutation.nodes) == 1
    assert mutation.edges == []


def test_anonymous_comparison_receipt_mismatch_is_gated_per_side() -> None:
    result = comparison_result(
        anonymous={
            "status_code": 401, "body_sha256": "a" * 64, "redirect": False,
            "actual_session_ref": None,
        },
        authenticated={
            "status_code": 200, "body_sha256": "b" * 64, "redirect": False,
            "actual_session_ref": "bob",
        },
    )
    mutation = map_web_compare_anonymous(
        target=EndpointTarget(host=HOST, path=PATH),
        arguments={"target_ref": HOST, "path": PATH, "session_ref": "alice"},
        result=result,
    )

    observation = mutation.nodes[0].observations[0]
    assert observation["anonymous_status"] == 401
    assert observation["authenticated_status"] == 200
    assert observation["anonymous_actual_session_ref"] is None
    assert "authenticated_actual_session_ref" not in observation
    assert len(mutation.nodes) == 1
    assert mutation.edges == []


def test_the_comparison_mapper_ignores_malformed_output() -> None:
    target = EndpointTarget(host=HOST, path=PATH)
    arguments = {"target_ref": HOST, "path": PATH, "session_ref": "alice"}
    cases = [
        comparison_result(anonymous="nope"),
        comparison_result(authenticated={}),
        comparison_result(anonymous={"status_code": True}),
        comparison_result(anonymous={"status_code": 999}),
        comparison_result(target_ref="different-target"),
        comparison_result(same_status=True),
        comparison_result(),
    ]

    for result in cases[:-1]:
        assert map_web_compare_anonymous(
            target=target, arguments=arguments, result=result
        ).nodes == []
    # Sanity: the well-formed case is not accidentally caught by the guard.
    assert map_web_compare_anonymous(
        target=target, arguments=arguments, result=cases[-1]
    ).nodes


def test_http_status_validation_rejects_booleans() -> None:
    assert is_http_status(200)
    assert not is_http_status(True)
    assert not is_http_status("200")
    assert not is_http_status(99)


# -- analysis / reporting are seams, not scheduled roles -------------------


def test_only_reporting_is_declared_as_a_non_schedulable_seam() -> None:
    seams = {
        str(item.agent_id) for item in builtin_agent_definitions() if not item.schedulable
    }

    assert seams == {"reporting-agent"}


def test_the_default_registry_schedules_only_executable_roles() -> None:
    registry = default_registry()

    assert {str(item) for item in registry.agent_ids} == {
        "recon-agent",
        "web-api-agent",
        "mobile-agent",
        "analysis-agent",
    }
    assert {str(item) for item in registry.definition_ids} >= {
        "analysis-agent",
        "reporting-agent",
    }


def test_analysis_has_an_executable_spec_and_reporting_does_not() -> None:
    registry = default_registry()

    assert registry.get(AgentId("analysis-agent")) is not None
    assert registry.get(AgentId("reporting-agent")) is None
    # Retirement of every executable role must not promote a seam role into a
    # scheduled one -- there is simply nothing left to choose.
    assert (
        registry.next_agent(
            current=None,
            finished=[
                AgentId("recon-agent"),
                AgentId("web-api-agent"),
                AgentId("mobile-agent"),
            ],
        )
        is None
    )


def test_every_declared_definition_resolves_to_a_registered_provider() -> None:
    from redteam.orchestration.agents.providers import default_provider_registry

    providers = default_provider_registry()

    for definition in builtin_agent_definitions():
        assert providers.get(definition.provider_id) is not None, definition.provider_id


def test_the_endpoint_facts_the_mapper_writes_are_the_ones_providers_read() -> None:
    # A guard against the two halves drifting: the web provider looks for
    # "observations" on the endpoint node, and update.py only attaches the
    # evidence id when the mapper declared one.
    mutation = map_web_compare_anonymous(
        target=EndpointTarget(host=HOST, path=PATH),
        arguments={"target_ref": HOST, "path": PATH, "session_ref": "alice"},
        result=comparison_result(),
    )
    node = mutation.nodes[0]

    assert node.facts["host"] == HOST
    assert facts_of({"facts": node.facts}).get("path") == PATH
    assert node.observations
