"""Each orchestration node in isolation, against Fakes for every port."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, cast

import pytest

from redteam.attack_graph.predicates import attrs_of, facts_of
from redteam.attack_graph.service import (
    HYPOTHESIS_CONFIRMED,
    HYPOTHESIS_INCONCLUSIVE,
    HYPOTHESIS_OPEN,
    HYPOTHESIS_REJECTED,
    AttackGraphService,
)
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.events import EventType
from redteam.core.contracts.graph import Hypothesis
from redteam.core.contracts.target import HostTarget
from redteam.core.contracts.tool import ToolError, ToolResult
from redteam.core.enums import AgentPhase, GraphEdgeType, GraphNodeType, PolicyVerdict, Verdict
from redteam.core.ids import (
    ActionId,
    AgentId,
    EngagementId,
    EvidenceId,
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
from redteam.orchestration.events import (
    AGENT_CONTEXT_PREPARED,
    AGENT_DECISION_RECORDED,
    CANDIDATE_GENERATED,
    CANDIDATE_REJECTED,
    GRAPH_MUTATION_APPLIED,
    HYPOTHESIS_RESOLVED,
    MCP_TOOL_SELECTED,
    OBSERVATION_RECORDED,
    REPLAN_TRIGGERED,
)
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
    # CandidateGenerated is published for the compiled frontier before the
    # hypothesis/action pair, so it belongs in the same ordered assertion.
    assert types == [
        EventType.AGENT_STARTED,
        CANDIDATE_GENERATED,
        AGENT_CONTEXT_PREPARED,
        AGENT_DECISION_RECORDED,
        EventType.HYPOTHESIS_CREATED,
        MCP_TOOL_SELECTED,
        EventType.ACTION_PROPOSED,
    ]


async def test_plan_produces_nothing_when_the_frontier_is_empty() -> None:
    d = deps()  # nothing seeded -> no in-scope host
    state = await plan_node(await observe_node(initial_graph_state(runtime()), deps=d), deps=d)

    assert state["action_proposal"] is None
    assert state["runtime"].phase is AgentPhase.REPLAN
    assert route_after_plan(state) == "replan"


async def test_llm_first_uses_model_action_without_calling_playbook_provider() -> None:
    from redteam.attack_graph.identity import make_node_id
    from redteam.core.contracts.tool import ToolDescriptor
    from redteam.core.enums import RiskLevel, ToolHealthStatus
    from redteam.llm.fake import FakeLLMClient
    from redteam.orchestration.agents.providers import (
        CandidateProviderRegistry,
        PlaybookCandidateProvider,
    )
    from redteam.orchestration.events import AGENT_TURN_DECIDED

    class MustNotRun:
        async def propose_graph(self, spec, context):
            raise AssertionError("playbook provider must be fallback-only in llm_first")

    host_id = str(make_node_id(GraphNodeType.HOST, "a.local"))
    llm = FakeLLMClient(
        canned_structured={
            "intent": "act",
            "response": "HTTP 응답 여부를 확인하겠습니다.",
            "decision_summary": "서비스 정보가 없어 수동 정찰이 먼저 필요합니다.",
            "assumptions": ["등록된 호스트만 사용합니다."],
            "actions": [
                {
                    "capability": "recon",
                    "operation": "service_discovery",
                    "target_node_id": host_id,
                    "tool_id": "recon.http_probe",
                    "arguments": {"path": "/"},
                    "rationale": "Confirm HTTP reachability",
                }
            ],
        }
    )
    tool = ToolDescriptor(
        tool_id=ToolId("recon.http_probe"),
        server_id="recon-passive",
        tool_name="http_probe",
        operations=["service_discovery"],
        target_types=["host"],
        capability_tags=["recon"],
        risk_level=RiskLevel.LOW,
        health_status=ToolHealthStatus.HEALTHY,
    )
    gateway = DummyToolGateway(tools=[tool])
    providers = CandidateProviderRegistry(
        [
            ("playbook:recon", MustNotRun()),
            ("playbook:web_api", PlaybookCandidateProvider()),
            ("playbook:mobile", PlaybookCandidateProvider()),
        ]
    )
    d = deps(
        llm_client=llm,
        tool_gateway=gateway,
        tool_catalog=gateway,
        candidate_providers=providers,
        settings=OrchestrationSettings(planning_mode="llm_first"),
    )
    await seeded(d, "a.local")

    planned = await plan_node(await observe_node(initial_graph_state(runtime()), deps=d), deps=d)

    assert planned["action_proposal"] is not None
    assert planned["action_proposal"].operation == "service_discovery"
    turn = d.event_bus.history(event_type=AGENT_TURN_DECIDED)[0]
    assert turn.payload["candidate_source"] == "llm_turn"
    assert turn.payload["response"] == "HTTP 응답 여부를 확인하겠습니다."
    event_types = d.event_bus.event_types()
    assert event_types.index(AGENT_CONTEXT_PREPARED) < event_types.index(AGENT_TURN_DECIDED)
    assert event_types.index(AGENT_TURN_DECIDED) < event_types.index(EventType.ACTION_PROPOSED)


async def test_llm_first_falls_back_to_recipe_when_model_action_cannot_compile() -> None:
    from redteam.llm.fake import FakeLLMClient
    from redteam.orchestration.agents.providers import (
        CandidateProviderRegistry,
        PlaybookCandidateProvider,
    )
    from redteam.orchestration.events import AGENT_TURN_DECIDED

    class CountingRecipe(PlaybookCandidateProvider):
        calls = 0

        async def propose_graph(self, spec, context):
            self.calls += 1
            return await super().propose_graph(spec, context)

    recipe = CountingRecipe()
    providers = CandidateProviderRegistry(
        [
            ("playbook:recon", recipe),
            ("playbook:web_api", PlaybookCandidateProvider()),
            ("playbook:mobile", PlaybookCandidateProvider()),
        ]
    )
    llm = FakeLLMClient(
        canned_structured={
            "intent": "act",
            "response": "새 대상을 확인하겠습니다.",
            "decision_summary": "모델이 제안한 대상을 검증합니다.",
            "actions": [
                {
                    "capability": "recon",
                    "operation": "service_discovery",
                    "target_node_id": "host:not-in-graph",
                }
            ],
        }
    )
    d = deps(
        llm_client=llm,
        candidate_providers=providers,
        settings=OrchestrationSettings(planning_mode="llm_first"),
    )
    await seeded(d, "a.local")

    planned = await plan_node(await observe_node(initial_graph_state(runtime()), deps=d), deps=d)

    assert recipe.calls == 1
    assert planned["action_proposal"] is not None
    turn = d.event_bus.history(event_type=AGENT_TURN_DECIDED)[0]
    assert turn.payload["candidate_source"] == "recipe_fallback"


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
    summary = d.event_bus.history(event_type=AGENT_NO_EXECUTABLE_CANDIDATES)[0].payload
    assert summary["total_drafts"] >= 1
    assert summary["rejected_count"] >= 1
    assert summary["rejection_reason_summary"]["capability_unavailable"] >= 1


async def test_plan_reports_a_prior_terminal_candidate_once_per_planning_turn() -> None:
    d = deps()
    await seeded(d, "a.local")
    observed = await observe_node(initial_graph_state(runtime()), deps=d)
    planned = await plan_node(observed, deps=d)
    work_id = planned["current_work_id"]
    assert work_id is not None
    assert await d.work_store.complete(
        work_id,
        owner=str(RUN),
        status=WorkStatus.SUCCEEDED,
        now=datetime.now(timezone.utc),
    )

    again = await plan_node(observed, deps=d)

    assert again["action_proposal"] is None
    prior = [
        event
        for event in d.event_bus.history(event_type=CANDIDATE_REJECTED)
        if event.payload["reason"] == "prior_terminal_execution"
    ]
    assert len(prior) == 1
    assert prior[0].payload["candidate_key"]


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
    mismatched = RECON_AGENT.playbook[0].model_copy(update={"operation": "endpoint_discovery"})
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
    await service.record_hypothesis(Hypothesis(hypothesis_id="hyp_1", description="reachable"))
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
    assert d.event_bus.event_types() == [EventType.EVIDENCE_CREATED, HYPOTHESIS_RESOLVED]
    assert (
        d.event_bus.history(event_type=HYPOTHESIS_RESOLVED)[0].payload["status"]
        == HYPOTHESIS_REJECTED
    )


async def test_a_failed_execution_without_a_follow_up_closes_the_hypothesis() -> None:
    # R7: an open hypothesis must correspond to a verification that can still
    # run. A non-repeatable step is permanently deduplicated by the Work
    # Ledger once terminal, so there is no follow-up left to ask -- the
    # question closes inconclusive with a traceable reason instead of
    # parking the run on something nothing will ask again.
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
    assert await hypothesis_status(service, "hyp_1") == HYPOTHESIS_INCONCLUSIVE
    assert (
        d.event_bus.history(event_type=HYPOTHESIS_RESOLVED)[0].payload["reason"]
        == "no_follow_up_verification_available"
    )


async def test_a_failed_execution_keeps_a_repeatable_hypothesis_open() -> None:
    # The complementary half of R7: when a real follow-up verification can
    # still run, the question stays open rather than being closed early.
    d = deps()
    service = AttackGraphService(d.graph_store)
    await service.record_hypothesis(Hypothesis(hypothesis_id="hyp_1", description="reachable"))
    state = state_with(
        runtime(),
        action_proposal=proposal(hypothesis_id=HypothesisId("hyp_1")),
        tool_result=tool_result(success=False),
        plan_step=RECON_AGENT.playbook[0].model_copy(update={"repeatable": True}),
    )

    await verify_node(state, deps=d)

    assert await hypothesis_status(service, "hyp_1") == HYPOTHESIS_OPEN
    assert (
        d.event_bus.history(event_type=HYPOTHESIS_RESOLVED)[0].payload["status"] == HYPOTHESIS_OPEN
    )


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
        HYPOTHESIS_RESOLVED,
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
    "body_sha256": "a" * 64,
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
    assert facts_of(service_node.properties)["application_protocol"] == "http"

    endpoint_node = await node_at(service, "endpoint:a.local GET /")
    assert attrs_of(endpoint_node.properties) == {"status_code": 200, "redirect": False}
    assert facts_of(endpoint_node.properties)["path"] == "/"

    edges = await service.list_edges()
    edge_types = {(e.type.value, str(e.source_id), str(e.target_id)) for e in edges}
    assert ("hosts", "host:a.local", "service:a.local/http") in edge_types
    assert ("exposes", "service:a.local/http", "endpoint:a.local GET /") in edge_types

    # Case E: the pre-existing single-event assertion's *sibling* still
    # holds -- the host-attrs write happens exactly as before, this is
    # additive: three more GRAPH_UPDATED events (one per discovered node)
    # plus the GraphMutationApplied summary for that batch.
    assert d.event_bus.event_types() == [EventType.GRAPH_UPDATED] * 3 + [GRAPH_MUTATION_APPLIED]


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


_HTML_INVENTORY_OUTPUT = {
    "target_ref": "bank",
    "references": [
        {
            "kind": "a",
            "path": "/accounts/123",
            "method": "GET",
            "query_omitted": False,
            "fragment_omitted": False,
            "path_allowlisted": True,
        }
    ],
    "reference_limit": 200,
    "references_truncated": False,
    "omitted_references": 0,
    "base_element_present": False,
    "decoding_replacements": False,
    "references_followed": False,
    "javascript_executed": False,
    "source_untrusted": True,
    "verdict": "observation_only",
    "source_sha256": "a" * 64,
    "request_count": 1,
}


async def test_inventory_update_merges_nodes_and_keeps_evidence_backed_history() -> None:
    d = deps()
    graph = AttackGraphService(d.graph_store)
    first_proposal = proposal().model_copy(
        update={"arguments": {"target_ref": "bank", "path": "/"}}
    )
    first = state_with(
        runtime(step_count=1),
        action_proposal=first_proposal,
        tool_result=tool_result(
            output=_HTML_INVENTORY_OUTPUT,
            tool_id="recon.html_inventory",
        ),
        result_evidence_id=EvidenceId("ev_html_1"),
        plan_step=RECON_AGENT.playbook[0],
    )
    await update_node(first, deps=d)

    second_proposal = proposal().model_copy(
        update={"arguments": {"target_ref": "bank", "path": "/index"}}
    )
    second = merged(
        first,
        runtime=runtime(step_count=2),
        action_proposal=second_proposal,
        result_evidence_id=EvidenceId("ev_html_2"),
    )
    await update_node(second, deps=d)

    endpoint = await node_at(graph, "endpoint:a.local GET /accounts/{id}")
    endpoint_facts = facts_of(endpoint.properties)
    assert endpoint_facts["evidence_ids"] == ["ev_html_1", "ev_html_2"]
    assert {item["source_path"] for item in endpoint_facts["observations"]} == {
        "/",
        "/index",
    }
    assert {item["evidence_id"] for item in endpoint_facts["observations"]} == {
        "ev_html_1",
        "ev_html_2",
    }
    endpoints = [node for node in await graph.list_nodes() if node.type is GraphNodeType.ENDPOINT]
    assert len(endpoints) == 1
    observed_edges = [
        edge for edge in await graph.list_edges() if edge.type is GraphEdgeType.OBSERVED
    ]
    assert len(observed_edges) == 1


async def test_proxy_update_attaches_evidence_to_each_captured_flow_observation() -> None:
    d = deps()
    graph = AttackGraphService(d.graph_store)
    flow_ref = "flow_0123456789abcdef"
    proxy_proposal = proposal().model_copy(
        update={"arguments": {"target_ref": "bank", "limit": 10}}
    )
    proxy_output = {
        "flows": [
            {
                "flow_ref": flow_ref,
                "target_ref": "bank",
                "method": "GET",
                "path": "/accounts/123",
                "request_json_fields": [],
                "request_body_omitted": False,
                "captured_response": {
                    "status_code": 200,
                    "body_bytes": 17,
                    "body_sha256": "a" * 64,
                    "redirect": False,
                    "security_headers_present": {
                        "strict-transport-security": True,
                        "content-security-policy": False,
                        "x-content-type-options": True,
                        "cache-control": False,
                    },
                    "body_omitted": True,
                },
            }
        ],
        "may_be_truncated": False,
    }
    state = state_with(
        runtime(step_count=1),
        action_proposal=proxy_proposal,
        tool_result=tool_result(output=proxy_output, tool_id="web.proxy_list_flows"),
        result_evidence_id=EvidenceId("ev_proxy_1"),
        plan_step=RECON_AGENT.playbook[0],
    )

    await update_node(state, deps=d)

    endpoint = await node_at(graph, "endpoint:a.local GET /accounts/{id}")
    endpoint_facts = facts_of(endpoint.properties)
    assert endpoint_facts["evidence_ids"] == ["ev_proxy_1"]
    assert endpoint_facts["observations"][0]["evidence_id"] == "ev_proxy_1"
    assert endpoint_facts["observations"][0]["captured_response"]["status_code"] == 200
    assert all(node.type is not GraphNodeType.FINDING for node in await graph.list_nodes())


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
    state = initial_graph_state(runtime(step_count=1, current_agent=RECON_AGENT.agent_id))

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


# -- P0-4 / P1-1 / P1-3: the feedback seams, reached through the nodes -------


def observation_step(**overrides) -> PlanStep:
    values: dict[str, Any] = {
        "step_id": "inspect",
        "capability": "web",
        "operation": "http_request",
        "tool_id": ToolId("web.http_client"),
        "target": HostTarget(host="a.local"),
        "reason": "record what the endpoint answers",
        "assessment_kind": "observation",
        "expectation": Expectation(
            kind="output_field_equals",
            field="status",
            value=200,
            violation_if_met=True,
        ),
    }
    values.update(overrides)
    return PlanStep(**values)


async def test_an_observation_only_step_never_becomes_a_finding() -> None:
    # Even an expectation that would be a violation cannot promote an
    # observation: assessment_kind=observation means no security claim was
    # under test in the first place.
    d = deps()
    service = AttackGraphService(d.graph_store)
    await service.record_hypothesis(Hypothesis(hypothesis_id="hyp_1", description="reachable"))
    state = state_with(
        runtime(),
        action_proposal=proposal(hypothesis_id=HypothesisId("hyp_1")),
        tool_result=tool_result(output={"status": 200}),
        plan_step=observation_step(),
    )

    result = await verify_node(state, deps=d)

    assert result["runtime"].last_verdict is Verdict.UNKNOWN
    assert await service.list_findings(verified=True) == []
    assert OBSERVATION_RECORDED in d.event_bus.event_types()
    assert EventType.FINDING_CREATED not in d.event_bus.event_types()


async def test_a_violation_with_no_hypothesis_under_test_is_refused_observably() -> None:
    d = deps()
    state = state_with(
        runtime(),
        action_proposal=proposal(),  # no hypothesis was stated before acting
        tool_result=tool_result(output={"status": 200}),
        plan_step=observation_step(assessment_kind="security_check"),
    )

    result = await verify_node(state, deps=d)

    assert result["runtime"].last_verdict is Verdict.UNKNOWN
    assert d.event_bus.history(event_type=EventType.FINDING_CREATED) == []
    assert (
        d.event_bus.history(event_type=OBSERVATION_RECORDED)[-1].payload[
            "finding_promotion_refused"
        ]
        == "no_hypothesis_under_test"
    )


async def test_plan_reports_a_step_that_cannot_fill_its_graph_arguments() -> None:
    # The web playbook can inspect a SERVICE, but a ServiceTarget has no path
    # and $path must never be guessed. Previously that step simply vanished;
    # now the drop names itself.
    d = deps()
    service = await seeded(d, "a.local")
    await service.upsert_node(
        GraphNodeType.SERVICE,
        key="a.local/http",
        attrs={"responded": True},
        facts={"scheme": "http", "host": "a.local"},
    )
    state = await observe_node(
        initial_graph_state(runtime(current_agent=AgentId("web-api-agent"))), deps=d
    )

    await plan_node(state, deps=d)

    rejected = d.event_bus.history(event_type=CANDIDATE_REJECTED)
    assert [(item.payload["reason"], item.payload["field"]) for item in rejected] == [
        ("graph_prerequisite_missing", "path")
    ]
    assert rejected[0].payload["operation"] == "inspect-http-service"


class _ProviderStub:
    """A candidate provider that returns exactly the batch it was given."""

    declared_operations: tuple[tuple[str, str], ...] = ()

    def __init__(self, batch) -> None:
        self._batch = batch

    async def propose_graph(self, spec, context):
        return self._batch


def _providers_with_web(replacement):
    from redteam.orchestration.agents.providers import (
        CandidateProviderRegistry,
        PlaybookCandidateProvider,
    )

    return CandidateProviderRegistry(
        [
            ("playbook:recon", PlaybookCandidateProvider()),
            ("playbook:web_api", replacement),
            ("playbook:mobile", PlaybookCandidateProvider()),
        ]
    )


async def test_a_drop_only_frontier_is_reported_as_no_executable_candidates() -> None:
    # The web playbook matched a real SERVICE but cannot fill its path, so it
    # produced a drop and no draft at all. Nothing executable came out, and
    # the reason must not stay silent just because drafts happened to be 0.
    from redteam.orchestration.events import AGENT_NO_EXECUTABLE_CANDIDATES

    d = deps()
    service = await seeded(d, "a.local")
    await service.upsert_node(
        GraphNodeType.SERVICE,
        key="a.local/http",
        attrs={"responded": True},
        facts={"scheme": "http", "host": "a.local"},
    )
    state = await observe_node(
        initial_graph_state(runtime(current_agent=AgentId("web-api-agent"))), deps=d
    )

    await plan_node(state, deps=d)

    summary = d.event_bus.history(event_type=AGENT_NO_EXECUTABLE_CANDIDATES)[0].payload
    assert summary["total_drafts"] == 0
    assert summary["drafts_offered"] == 0
    assert summary["dropped_count"] >= 1
    assert summary["rejected_count"] == summary["dropped_count"]
    assert summary["rejection_reason_summary"]["graph_prerequisite_missing"] >= 1


async def test_a_diagnostic_only_provider_is_reported_as_no_executable_candidates() -> None:
    from redteam.core.ids import GraphNodeId
    from redteam.orchestration.agents.providers import (
        CandidateBatch,
        CandidateDiagnostic,
    )
    from redteam.orchestration.events import AGENT_NO_EXECUTABLE_CANDIDATES

    batch = CandidateBatch(
        candidates=[],
        diagnostics=[
            CandidateDiagnostic(
                target_node_id=GraphNodeId("endpoint:a.local GET /"),
                reason="no_registered_session_for_path",
                evidence_id="ev_diag",
            )
        ],
    )
    d = deps(candidate_providers=_providers_with_web(_ProviderStub(batch)))
    service = await seeded(d, "a.local")
    await service.upsert_node(
        GraphNodeType.SERVICE,
        key="a.local/http",
        attrs={"responded": True},
        facts={"scheme": "http", "host": "a.local"},
    )
    state = await observe_node(
        initial_graph_state(runtime(current_agent=AgentId("web-api-agent"))), deps=d
    )

    await plan_node(state, deps=d)

    summary = d.event_bus.history(event_type=AGENT_NO_EXECUTABLE_CANDIDATES)[0].payload
    assert summary["total_drafts"] == 0
    assert summary["dropped_count"] == 0
    assert summary["provider_diagnostic_count"] == 1
    assert summary["provider_diagnostic_summary"]["no_registered_session_for_path"] == 1
    assert summary["rejected_count"] == 0


async def test_a_provider_with_nothing_to_say_stays_silent() -> None:
    # "Nothing to do" is genuinely different from "something was considered
    # and none of it was executable", and must not start emitting events.
    from redteam.orchestration.agents.providers import CandidateBatch
    from redteam.orchestration.events import AGENT_NO_EXECUTABLE_CANDIDATES

    d = deps(candidate_providers=_providers_with_web(_ProviderStub(CandidateBatch(candidates=[]))))
    service = await seeded(d, "a.local")
    await service.upsert_node(
        GraphNodeType.SERVICE,
        key="a.local/http",
        attrs={"responded": True},
        facts={"scheme": "http", "host": "a.local"},
    )
    state = await observe_node(
        initial_graph_state(runtime(current_agent=AgentId("web-api-agent"))), deps=d
    )

    await plan_node(state, deps=d)

    assert d.event_bus.history(event_type=AGENT_NO_EXECUTABLE_CANDIDATES) == []


async def test_replan_names_a_terminal_decision() -> None:
    d = deps()
    state = initial_graph_state(runtime(step_count=1, current_agent=RECON_AGENT.agent_id))

    await replan_node(state, deps=d)

    triggered = d.event_bus.history(event_type=REPLAN_TRIGGERED)
    assert len(triggered) == 1
    assert triggered[0].payload["outcome"] == "terminate"
    assert triggered[0].payload["reason"] == "terminal_condition"


async def test_replan_stays_silent_when_it_is_only_taking_another_turn() -> None:
    # "We took a turn" is not a replanning event; publishing one per step would
    # bury the decisions that actually matter.
    d = deps()
    state = state_with(runtime(step_count=1), action_proposal=proposal())

    await replan_node(state, deps=d)

    assert d.event_bus.history(event_type=REPLAN_TRIGGERED) == []


async def test_replan_maps_a_hard_stop_to_its_own_reason() -> None:
    d = deps(settings=OrchestrationSettings(max_steps=4))
    state = state_with(
        runtime(step_count=4, current_agent=RECON_AGENT.agent_id), action_proposal=proposal()
    )

    await replan_node(state, deps=d)

    triggered = d.event_bus.history(event_type=REPLAN_TRIGGERED)
    assert [item.payload["reason"] for item in triggered] == ["max_steps"]
