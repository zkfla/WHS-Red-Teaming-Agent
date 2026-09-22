"""Each orchestration node in isolation, against Fakes for every port."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, cast

import pytest

from redteam.attack_graph.predicates import attrs_of, facts_of
from redteam.attack_graph.service import (
    HYPOTHESIS_CONFIRMED,
    HYPOTHESIS_OPEN,
    HYPOTHESIS_REJECTED,
    AttackGraphService,
)
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.events import EventType
from redteam.core.contracts.graph import Hypothesis
from redteam.core.contracts.target import HostTarget
from redteam.core.contracts.tool import ToolError, ToolResult
from redteam.core.enums import AgentPhase, GraphNodeType, PolicyVerdict, Verdict
from redteam.core.ids import (
    ActionId,
    AgentId,
    EngagementId,
    GraphNodeId,
    HypothesisId,
    InvocationId,
    RunId,
    ToolId,
)
from redteam.event_bus.memory import InMemoryEventBus
from redteam.orchestration.agents import AgentRegistry, AgentSpec, HypothesisDraft, PlanStep
from redteam.orchestration.agents.registry import RECON_AGENT
from redteam.orchestration.candidates.compiler import CandidateCompiler
from redteam.orchestration.expectations import Expectation
from redteam.orchestration.nodes import (
    execute_node,
    observe_node,
    plan_node,
    policy_node,
    replan_node,
    update_node,
    verify_node,
)
from redteam.orchestration.router import (
    route_after_observe,
    route_after_plan,
    route_after_policy,
    route_after_replan,
)
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import (
    GraphState,
    RuntimeState,
    TerminationReason,
    initial_graph_state,
)
from redteam.orchestration.work.models import WorkItem, WorkStatus
from tests.fakes import DummyToolGateway, FakeControlPlane, build_test_application

NOW = datetime.now(timezone.utc)
RUN = RunId("run_1")
ENGAGEMENT = EngagementId("eng_1")


def runtime(**overrides) -> RuntimeState:
    return RuntimeState(run_id=RUN, engagement_id=ENGAGEMENT, **overrides)


def deps(**overrides: Any):
    overrides.setdefault("event_bus", InMemoryEventBus())
    return build_test_application(**overrides)


def state_with(rt: RuntimeState | None = None, **transient: Any) -> GraphState:
    """A GraphState with some transient fields pre-filled."""
    base: dict[str, Any] = dict(initial_graph_state(rt if rt is not None else runtime()))
    base.update(transient)
    return cast(GraphState, base)


def merged(state: GraphState, **transient: Any) -> GraphState:
    return cast(GraphState, {**state, **transient})


async def node_at(service: AttackGraphService, node_id: str):
    node = await service.get_node(GraphNodeId(node_id))
    assert node is not None
    return node


async def hypothesis_status(service: AttackGraphService, hypothesis_id: str) -> str:
    hypothesis = await service.get_hypothesis(hypothesis_id)
    assert hypothesis is not None
    return hypothesis.status


async def seeded(deps_obj, *hosts: str) -> AttackGraphService:
    service = AttackGraphService(deps_obj.graph_store)
    for host in hosts:
        await service.upsert_node(GraphNodeType.HOST, key=host, attrs={"in_scope": True})
    return service


def proposal(hypothesis_id: HypothesisId | None = None, host: str = "a.local") -> ActionProposal:
    return ActionProposal(
        action_id=ActionId("action_1"),
        run_id=RUN,
        engagement_id=ENGAGEMENT,
        agent_id=AgentId("recon-agent"),
        capability="recon",
        operation="service_discovery",
        target=HostTarget(host=host),
        reason="probe",
        hypothesis_id=hypothesis_id,
        expected_observation="echo comes back",
        created_at=NOW,
    )


def tool_result(
    success: bool = True,
    output: dict[str, Any] | str | None = None,
    tool_id: str = "recon.ping",
) -> ToolResult:
    return ToolResult(
        invocation_id=InvocationId("inv_1"),
        action_id=ActionId("action_1"),
        tool_id=ToolId(tool_id),
        success=success,
        output=output if output is not None else {"echo": {"message": "hi"}},
        error=None if success else ToolError(code="timeout", message="no answer"),
        started_at=NOW,
        finished_at=NOW,
    )


# -- OBSERVE --------------------------------------------------------------


async def test_observe_schedules_an_agent_and_announces_it_once() -> None:
    d = deps()
    await seeded(d, "a.local")
    state = initial_graph_state(runtime())

    first = await observe_node(state, deps=d)
    assert first["runtime"].current_agent == RECON_AGENT.agent_id
    assert first["runtime"].phase is AgentPhase.PLAN
    assert first["runtime"].step_count == 1
    observation = first["observation"]
    assert observation is not None and observation.node_counts == {"host": 1}
    assert d.event_bus.event_types() == [EventType.AGENT_STARTED]

    second = await observe_node(first, deps=d)
    # Same agent continuing: no second AgentStarted.
    assert d.event_bus.event_types() == [EventType.AGENT_STARTED]
    assert second["runtime"].step_count == 2


async def test_observe_routes_to_replan_when_no_agent_is_left() -> None:
    d = deps(agents=AgentRegistry([]))
    state = await observe_node(initial_graph_state(runtime()), deps=d)

    assert state["runtime"].phase is AgentPhase.REPLAN
    assert state["runtime"].termination_reason == TerminationReason.NO_AGENTS
    assert route_after_observe(state) == "replan"


async def test_observe_distinguishes_inactive_agents_from_an_empty_registry() -> None:
    d = deps()  # Built-in agents exist, but an empty graph activates none.
    state = await observe_node(initial_graph_state(runtime()), deps=d)

    assert state["runtime"].phase is AgentPhase.REPLAN
    assert state["runtime"].termination_reason == TerminationReason.NO_ACTIONABLE_PLAN


# -- PLAN -----------------------------------------------------------------


async def test_plan_states_a_hypothesis_before_it_acts() -> None:
    d = deps()
    service = await seeded(d, "a.local")
    state = await plan_node(await observe_node(initial_graph_state(runtime()), deps=d), deps=d)

    action = state["action_proposal"]
    assert action is not None
    assert action.capability == "recon"
    assert isinstance(action.target, HostTarget)
    assert action.target.host == "a.local"
    assert state["runtime"].phase is AgentPhase.POLICY
    assert state["runtime"].pending_action_id == action.action_id

    # The hypothesis, and the test that will decide it, are in the graph
    # before the tool ever runs.
    hypotheses = await service.list_hypotheses(status=HYPOTHESIS_OPEN)
    assert len(hypotheses) == 1
    assert hypotheses[0].metadata["expectation"]["kind"] == "tool_succeeded"
    assert state["runtime"].active_hypothesis_id == hypotheses[0].hypothesis_id
    assert route_after_plan(state) == "policy"

    types = d.event_bus.event_types()
    assert types == [
        EventType.AGENT_STARTED,
        EventType.HYPOTHESIS_CREATED,
        EventType.ACTION_PROPOSED,
    ]


async def test_plan_produces_nothing_when_the_frontier_is_empty() -> None:
    d = deps()  # nothing seeded -> no in-scope host
    state = await plan_node(await observe_node(initial_graph_state(runtime()), deps=d), deps=d)

    assert state["action_proposal"] is None
    assert state["runtime"].phase is AgentPhase.REPLAN
    assert route_after_plan(state) == "replan"


async def test_plan_warns_when_an_activated_agent_gets_zero_executable_candidates() -> None:
    """Section 10: distinguishable from ordinary silence -- the agent was
    activated (a real ENDPOINT matched its precondition) but no tool is
    registered for its capability/operation at all, so every draft is
    dropped during compilation."""
    from redteam.attack_graph.identity import endpoint_node_id
    from redteam.orchestration.events import AGENT_NO_EXECUTABLE_CANDIDATES

    gateway = DummyToolGateway(tools=[])  # no tool registered for anything
    d = deps(tool_catalog=gateway, tool_gateway=gateway)
    service = AttackGraphService(d.graph_store)
    await service.upsert_node(
        GraphNodeType.ENDPOINT, node_id=endpoint_node_id("GET", "/", host="a.local")
    )

    observed = await observe_node(initial_graph_state(runtime()), deps=d)
    assert observed["runtime"].current_agent == AgentId("web-api-agent")

    state = await plan_node(observed, deps=d)

    assert state["action_proposal"] is None
    assert AGENT_NO_EXECUTABLE_CANDIDATES in d.event_bus.event_types()


async def test_plan_without_a_hypothesis_still_proposes() -> None:
    step = PlanStep(
        step_id="bare",
        capability="recon",
        operation="service_discovery",
        tool_id=ToolId("recon.ping"),
        target=HostTarget(host="a.local"),
        reason="no hypothesis on this one",
    )
    agent = AgentSpec(agent_id=AgentId("bare"), name="bare", playbook=[step])
    d = deps(agents=AgentRegistry([agent]))
    state = await plan_node(await observe_node(initial_graph_state(runtime()), deps=d), deps=d)

    proposed = state["action_proposal"]
    assert proposed is not None and proposed.hypothesis_id is None
    assert EventType.HYPOTHESIS_CREATED not in d.event_bus.event_types()


async def test_plan_reacquires_a_previously_pending_candidate() -> None:
    d = deps()
    service = await seeded(d, "a.local")
    observed = await observe_node(initial_graph_state(runtime()), deps=d)
    observation = observed["observation"]
    assert observation is not None
    compiled = await CandidateCompiler().compile_playbook(
        RECON_AGENT,
        service.view(),
        observation,
        step=1,
    )
    candidate = compiled[0]
    await d.work_store.add(
        WorkItem(
            work_id="work_pending_from_prior_tick",
            run_id=RUN,
            candidate_key=candidate.candidate_key,
            agent_id=RECON_AGENT.agent_id,
            graph_revision=1,
            target_node_ids=candidate.draft.target_node_ids,
            hypothesis_id=HypothesisId("hyp_pending_from_prior_tick"),
            priority=candidate.priority,
            created_at=NOW,
            updated_at=NOW,
        )
    )

    planned = await plan_node(observed, deps=d)

    assert planned["current_work_id"] == "work_pending_from_prior_tick"
    restored = await d.work_store.get("work_pending_from_prior_tick")
    assert restored is not None and restored.status is WorkStatus.LEASED


# -- POLICY ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("verdict", "event", "phase", "edge"),
    [
        (PolicyVerdict.ALLOW, EventType.ACTION_APPROVED, AgentPhase.EXECUTE, "execute"),
        (PolicyVerdict.DENY, EventType.ACTION_DENIED, AgentPhase.REPLAN, "update"),
        (
            PolicyVerdict.REQUIRE_APPROVAL,
            EventType.APPROVAL_REQUIRED,
            AgentPhase.REPLAN,
            "update",
        ),
    ],
)
async def test_policy_routes_on_the_control_plane_verdict(verdict, event, phase, edge) -> None:
    d = deps(control_plane=FakeControlPlane(default_verdict=verdict))
    state = state_with(runtime(), action_proposal=proposal())

    result = await policy_node(state, deps=d)

    assert result["runtime"].phase is phase
    assert d.event_bus.event_types() == [EventType.POLICY_EVALUATED, event]
    # Only ALLOW reaches the Capability Layer; a block still goes through
    # UPDATE so the graph records that this action was not taken.
    assert route_after_policy(result) == edge


async def test_a_blocked_action_is_recorded_without_asserting_anything() -> None:
    d = deps(control_plane=FakeControlPlane(default_verdict=PolicyVerdict.DENY))
    service = AttackGraphService(d.graph_store)
    state = state_with(runtime(), action_proposal=proposal())
    state = await policy_node(state, deps=d)
    state = merged(state, plan_step=RECON_AGENT.playbook[0])

    await update_node(state, deps=d)

    node = await node_at(service, "host:a.local")
    assert attrs_of(node.properties) == {}
    facts = facts_of(node.properties)
    assert facts["last_operation_blocked"] is True
    assert facts["last_block_verdict"] == "deny"
    # Recorded as attempted, so the planner does not re-propose it forever.
    assert facts["attempted_operations"] == ["recon.service_discovery"]
    assert d.event_bus.history(event_type=EventType.GRAPH_UPDATED)[0].payload["blocked"] is True


async def test_policy_correlates_every_event_to_the_action() -> None:
    d = deps()
    state = state_with(runtime(), action_proposal=proposal())
    await policy_node(state, deps=d)

    assert len(d.event_bus.correlated("action_1")) == 2


# -- EXECUTE --------------------------------------------------------------


async def test_execute_uses_the_tool_the_chosen_step_declared() -> None:
    gateway = DummyToolGateway()
    d = deps(tool_gateway=gateway)
    state = state_with(runtime(remaining_budget=3), action_proposal=proposal())
    state = await policy_node(state, deps=d)
    state = merged(state, plan_step=RECON_AGENT.playbook[0])

    result = await execute_node(state, deps=d)

    assert gateway.invocations[0].tool_id == ToolId("recon.probe")
    assert result["runtime"].actions_executed == 1
    assert result["runtime"].remaining_budget == 2
    assert result["runtime"].consecutive_failures == 0
    assert EventType.TOOL_EXECUTION_FINISHED in d.event_bus.event_types()


async def test_execute_counts_consecutive_failures() -> None:
    d = deps(tool_gateway=DummyToolGateway(success=False))
    state = state_with(runtime(consecutive_failures=1), action_proposal=proposal())
    state = await policy_node(state, deps=d)
    state = merged(state, plan_step=RECON_AGENT.playbook[0])

    result = await execute_node(state, deps=d)

    assert result["runtime"].consecutive_failures == 2
    assert EventType.TOOL_EXECUTION_FAILED in d.event_bus.event_types()


async def test_execute_does_not_invoke_tool_after_lease_is_lost() -> None:
    gateway = DummyToolGateway()
    d = deps(tool_gateway=gateway)
    state = state_with(
        runtime(remaining_budget=3),
        action_proposal=proposal(),
        current_work_id="missing-work",
    )
    state = await policy_node(state, deps=d)
    state = merged(state, plan_step=RECON_AGENT.playbook[0])

    result = await execute_node(state, deps=d)

    assert gateway.invocations == []
    tool = result["tool_result"]
    assert tool is not None and tool.success is False
    assert tool.error is not None and tool.error.code == "lease_lost_before_execution"
    assert tool.metadata["orchestration_exception"] is True


async def test_execute_rejects_a_tool_binding_for_a_different_approved_intent() -> None:
    gateway = DummyToolGateway()
    d = deps(tool_gateway=gateway)
    state = state_with(runtime(), action_proposal=proposal())
    state = await policy_node(state, deps=d)
    mismatched = RECON_AGENT.playbook[0].model_copy(
        update={"operation": "endpoint_discovery"}
    )
    state = merged(state, plan_step=mismatched)

    result = await execute_node(state, deps=d)

    assert gateway.invocations == []
    tool = result["tool_result"]
    assert tool is not None and tool.success is False
    assert tool.error is not None and tool.error.code == "tool_binding_invalid"


# -- VERIFY ---------------------------------------------------------------


async def test_verify_records_evidence_then_reaches_a_verdict() -> None:
    d = deps()
    service = AttackGraphService(d.graph_store)
    await service.record_hypothesis(
        Hypothesis(hypothesis_id="hyp_1", description="reachable")
    )
    state = state_with(
        runtime(),
        action_proposal=proposal(hypothesis_id=HypothesisId("hyp_1")),
        tool_result=tool_result(),
        plan_step=RECON_AGENT.playbook[0],
    )

    result = await verify_node(state, deps=d)

    assert d.evidence_store.records[0].action_id == ActionId("action_1")
    assert result["runtime"].last_verdict is Verdict.PASS
    assert await hypothesis_status(service, "hyp_1") == HYPOTHESIS_REJECTED
    assert d.event_bus.event_types() == [EventType.EVIDENCE_CREATED]


async def test_a_failed_execution_leaves_the_hypothesis_open() -> None:
    d = deps()
    service = AttackGraphService(d.graph_store)
    await service.record_hypothesis(Hypothesis(hypothesis_id="hyp_1", description="reachable"))
    state = state_with(
        runtime(),
        action_proposal=proposal(hypothesis_id=HypothesisId("hyp_1")),
        tool_result=tool_result(success=False),
        plan_step=RECON_AGENT.playbook[0],
    )

    result = await verify_node(state, deps=d)

    assert result["runtime"].last_verdict is Verdict.UNKNOWN
    assert await hypothesis_status(service, "hyp_1") == HYPOTHESIS_OPEN


async def test_a_violation_produces_a_verified_finding_backed_by_evidence() -> None:
    step = PlanStep(
        step_id="authz",
        capability="web",
        operation="http_request",
        tool_id=ToolId("web.http_client"),
        target=HostTarget(host="a.local"),
        reason="unauthenticated read must not succeed",
        hypothesis=HypothesisDraft(description="anonymous read works", violation_if_confirmed=True),
        expectation=Expectation(
            kind="output_field_equals",
            field="status",
            value=200,
            violation_if_met=True,
            description="anonymous caller must not get 200",
        ),
    )
    d = deps()
    service = AttackGraphService(d.graph_store)
    await service.record_hypothesis(Hypothesis(hypothesis_id="hyp_1", description="anon read"))
    state = state_with(
        runtime(),
        action_proposal=proposal(hypothesis_id=HypothesisId("hyp_1")),
        tool_result=tool_result(output={"status": 200}),
        plan_step=step,
    )

    result = await verify_node(state, deps=d)

    assert result["runtime"].last_verdict is Verdict.VIOLATION
    findings = await service.list_findings(verified=True)
    assert len(findings) == 1
    assert findings[0].evidence_ids == [str(d.evidence_store.records[0].evidence_id)]
    assert findings[0].severity == "high"
    assert await hypothesis_status(service, "hyp_1") == HYPOTHESIS_CONFIRMED
    assert d.event_bus.event_types() == [
        EventType.EVIDENCE_CREATED,
        EventType.FINDING_CREATED,
        EventType.FINDING_VERIFIED,
    ]
    # The graph stays reportable: no unfalsifiable claim got in.
    assert (await service.integrity_report()).ungrounded_findings == []


# -- UPDATE ---------------------------------------------------------------


async def test_update_writes_predicates_and_records_the_attempt() -> None:
    d = deps()
    service = AttackGraphService(d.graph_store)
    state = state_with(
        runtime(step_count=1),
        action_proposal=proposal(),
        tool_result=tool_result(),
        plan_step=RECON_AGENT.playbook[0],
    )

    result = await update_node(state, deps=d)

    node = await node_at(service, "host:a.local")
    assert attrs_of(node.properties) == {"reachable": True, "probed": True}
    assert facts_of(node.properties)["attempted_operations"] == ["recon.service_discovery"]
    # The payload itself stays in Evidence, not in long-lived graph state.
    assert "echo" not in facts_of(node.properties)
    assert result["runtime"].phase is AgentPhase.REPLAN
    assert d.event_bus.event_types() == [EventType.GRAPH_UPDATED]


async def test_update_does_not_assert_predicates_after_a_failed_tool_call() -> None:
    d = deps()
    service = AttackGraphService(d.graph_store)
    state = state_with(
        runtime(),
        action_proposal=proposal(),
        tool_result=tool_result(success=False),
        plan_step=RECON_AGENT.playbook[0],
    )

    await update_node(state, deps=d)

    node = await node_at(service, "host:a.local")
    assert attrs_of(node.properties) == {}
    # The attempt is still recorded, so it is not proposed again.
    assert facts_of(node.properties)["attempted_operations"] == ["recon.service_discovery"]


# -- UPDATE: Tool Result -> Attack Graph Mutation (recon.http_probe) ------


_HTTP_PROBE_OUTPUT = {
    "target_ref": "a.local",
    "status_code": 200,
    "body_bytes": 42,
    "body_sha256": "deadbeef",
    "redirect": False,
    "security_headers_present": {"strict-transport-security": False},
    "body_omitted": True,
}


async def test_update_discovers_service_and_endpoint_from_a_successful_http_probe() -> None:
    d = deps()
    service = AttackGraphService(d.graph_store)
    state = state_with(
        runtime(step_count=1),
        action_proposal=proposal(),
        tool_result=tool_result(output=_HTTP_PROBE_OUTPUT, tool_id="recon.http_probe"),
        plan_step=RECON_AGENT.playbook[0],
    )

    await update_node(state, deps=d)

    host_node = await node_at(service, "host:a.local")
    assert attrs_of(host_node.properties) == {"reachable": True, "probed": True}

    service_node = await node_at(service, "service:a.local/http")
    assert attrs_of(service_node.properties) == {"responded": True}
    assert facts_of(service_node.properties)["scheme"] == "http"

    endpoint_node = await node_at(service, "endpoint:a.local GET /")
    assert attrs_of(endpoint_node.properties) == {"status_code": 200, "redirect": False}
    assert facts_of(endpoint_node.properties)["path"] == "/"

    edges = await service.list_edges()
    edge_types = {(e.type.value, str(e.source_id), str(e.target_id)) for e in edges}
    assert ("hosts", "host:a.local", "service:a.local/http") in edge_types
    assert ("exposes", "service:a.local/http", "endpoint:a.local GET /") in edge_types

    # Case E: the pre-existing single-event assertion's *sibling* still
    # holds -- the host-attrs write happens exactly as before, this is
    # additive (three more GRAPH_UPDATED events, one per discovered node).
    assert d.event_bus.event_types() == [EventType.GRAPH_UPDATED] * 3


async def test_update_processing_the_same_probe_result_twice_does_not_duplicate_nodes() -> None:
    d = deps()
    service = AttackGraphService(d.graph_store)
    state = state_with(
        runtime(step_count=1),
        action_proposal=proposal(),
        tool_result=tool_result(output=_HTTP_PROBE_OUTPUT, tool_id="recon.http_probe"),
        plan_step=RECON_AGENT.playbook[0],
    )

    await update_node(state, deps=d)
    await update_node(merged(state, runtime=runtime(step_count=2)), deps=d)

    nodes = await service.list_nodes()
    service_nodes = [n for n in nodes if n.type is GraphNodeType.SERVICE]
    endpoint_nodes = [n for n in nodes if n.type is GraphNodeType.ENDPOINT]
    assert len(service_nodes) == 1
    assert len(endpoint_nodes) == 1

    edges = await service.list_edges()
    assert len(edges) == 2  # HOSTS + EXPOSES, not duplicated


async def test_update_does_not_invent_a_service_from_a_failed_probe() -> None:
    d = deps()
    service = AttackGraphService(d.graph_store)
    state = state_with(
        runtime(),
        action_proposal=proposal(),
        tool_result=tool_result(success=False, tool_id="recon.http_probe"),
        plan_step=RECON_AGENT.playbook[0],
    )

    await update_node(state, deps=d)

    nodes = await service.list_nodes()
    assert all(n.type is GraphNodeType.HOST for n in nodes)


async def test_update_ignores_malformed_probe_output_without_crashing() -> None:
    d = deps()
    service = AttackGraphService(d.graph_store)
    state = state_with(
        runtime(),
        action_proposal=proposal(),
        tool_result=tool_result(output={"no_status_code_here": True}, tool_id="recon.http_probe"),
        plan_step=RECON_AGENT.playbook[0],
    )

    await update_node(state, deps=d)  # must not raise

    nodes = await service.list_nodes()
    assert all(n.type is GraphNodeType.HOST for n in nodes)


async def test_discovered_service_and_endpoint_activate_the_web_api_agent() -> None:
    from redteam.orchestration.agents.definition import builtin_agent_definitions

    d = deps()
    service = AttackGraphService(d.graph_store)
    state = state_with(
        runtime(step_count=1),
        action_proposal=proposal(),
        tool_result=tool_result(output=_HTTP_PROBE_OUTPUT, tool_id="recon.http_probe"),
        plan_step=RECON_AGENT.playbook[0],
    )
    await update_node(state, deps=d)

    web_api_definition = next(
        definition
        for definition in builtin_agent_definitions()
        if str(definition.agent_id) == "web-api-agent"
    )
    observation = await service.observe(step=1)
    assert web_api_definition.is_active(observation, AgentPhase.OBSERVE)


# -- REPLAN ---------------------------------------------------------------


async def test_replan_continues_while_work_remains() -> None:
    d = deps()
    state = state_with(runtime(step_count=1), action_proposal=proposal())

    result = await replan_node(state, deps=d)

    assert result["runtime"].phase is AgentPhase.OBSERVE
    assert result["runtime"].termination_reason is None
    assert result["action_proposal"] is None
    assert route_after_replan(result) == "observe"


async def test_replan_retires_an_agent_with_nothing_left_and_hands_over() -> None:
    other = AgentSpec(agent_id=AgentId("web"), name="web")
    d = deps(agents=AgentRegistry([RECON_AGENT, other]))
    state = initial_graph_state(
        runtime(step_count=1, current_agent=RECON_AGENT.agent_id)
    )

    result = await replan_node(state, deps=d)

    assert result["runtime"].finished_agents == [RECON_AGENT.agent_id]
    assert result["runtime"].phase is AgentPhase.OBSERVE
    assert EventType.AGENT_FINISHED in d.event_bus.event_types()


async def test_replan_ends_the_run_when_every_agent_is_finished() -> None:
    d = deps()
    state = initial_graph_state(runtime(step_count=1, current_agent=RECON_AGENT.agent_id))

    result = await replan_node(state, deps=d)

    assert result["runtime"].phase is AgentPhase.FINISHED
    assert result["runtime"].termination_reason == TerminationReason.NO_ACTIONABLE_PLAN
    assert route_after_replan(result) == "end"


@pytest.mark.parametrize(
    ("state_kwargs", "settings", "expected"),
    [
        (
            {"step_count": 4},
            OrchestrationSettings(max_steps=4),
            TerminationReason.MAX_STEPS_REACHED,
        ),
        (
            {"step_count": 1, "remaining_budget": 0},
            OrchestrationSettings(),
            TerminationReason.BUDGET_EXHAUSTED,
        ),
        (
            {"step_count": 1, "consecutive_failures": 3},
            OrchestrationSettings(max_consecutive_failures=3),
            TerminationReason.CONSECUTIVE_FAILURES,
        ),
    ],
)
async def test_every_hard_stop_names_itself(state_kwargs, settings, expected) -> None:
    # "The run ended" and "the run ran out of budget after two turns"
    # lead to very different reports.
    d = deps(settings=settings)
    state = state_with(
        runtime(current_agent=RECON_AGENT.agent_id, **state_kwargs),
        action_proposal=proposal(),
    )

    result = await replan_node(state, deps=d)

    assert result["runtime"].phase is AgentPhase.FINISHED
    assert result["runtime"].termination_reason == expected
