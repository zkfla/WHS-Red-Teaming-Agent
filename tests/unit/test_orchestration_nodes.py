"""Each orchestration node in isolation, against Fakes for every port."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, cast

import pytest

from redteam.attack_graph.identity import host_node_id as _host_node_id_for_fixture
from redteam.attack_graph.predicates import attrs_of, facts_of
from redteam.attack_graph.service import (
    HYPOTHESIS_CONFIRMED,
    HYPOTHESIS_INCONCLUSIVE,
    HYPOTHESIS_OPEN,
    AttackGraphService,
)
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.events import EventType
from redteam.core.contracts.graph import Hypothesis
from redteam.core.contracts.target import AssetTarget, HostTarget
from redteam.core.contracts.tool import ToolDescriptor as _FixtureToolDescriptor
from redteam.core.contracts.tool import ToolError, ToolResult
from redteam.core.enums import AgentPhase, GraphEdgeType, GraphNodeType, PolicyVerdict, Verdict
from redteam.core.enums import RiskLevel as _FixtureRisk
from redteam.core.enums import ToolHealthStatus as _FixtureHealth
from redteam.core.ids import (
    ActionId,
    AgentId,
    AssetId,
    EngagementId,
    EvidenceId,
    GraphNodeId,
    HypothesisId,
    InvocationId,
    RunId,
    ToolId,
)
from redteam.event_bus.memory import InMemoryEventBus
from redteam.orchestration.agents import (
    ANALYSIS_AGENT,
    AgentRegistry,
    HypothesisDraft,
    PlanStep,
)
from redteam.orchestration.agents.registry import RECON_AGENT
from redteam.orchestration.events import (
    GRAPH_MUTATION_APPLIED,
    HYPOTHESIS_RESOLVED,
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
from redteam.orchestration.paths import CompletionCriterion as _FixtureCriterion
from redteam.orchestration.paths import PathPlan as _FixturePathPlan
from redteam.orchestration.paths import PathStep as _FixtureRegisteredStep
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
from redteam.orchestration.work.models import WorkStatus
from tests.fakes import DummyToolGateway, FakeControlPlane, build_test_application

NOW = datetime.now(timezone.utc)
RUN = RunId("run_1")
ENGAGEMENT = EngagementId("eng_1")

# -- A6 migration helpers: real active PathPlan + scripted LLM -----------------
# New contract: POLICY/EXECUTE deny without an accepted investigation path.
# These helpers register a real active path so tests exercise the real gate
# instead of the old bare-proposal shape. LLM paths use an explicit scripted
# stand-in (no network, no rule fallback).

_A6_PATH_ID = "path-a6-fixture"
_A6_STEP_ID = "probe-host"


async def _seed_known(d, host: str = "a.local"):
    # Production seeding path so the host lands in known_targets
    # (server-declared + target_ref). Simple upsert lacks provenance
    # and leaves KNOWN empty, which new engine compile check rejects.
    from redteam.core.contracts.engagement import (
        ApprovalPolicy,
        Engagement,
        EngagementScope,
        ExecutionBudget,
        RateLimitPolicy,
    )
    from redteam.orchestration.seed import seed_from_engagement

    eng = Engagement(
        engagement_id=ENGAGEMENT,
        name="a6 fixture",
        scope=EngagementScope(allowed_hosts=[host]),
        rules_of_engagement=[],
        execution_budget=ExecutionBudget(max_actions=10),
        rate_limit_policy=RateLimitPolicy(
            max_actions_per_minute=30, max_concurrent_actions=2
        ),
        approval_policy=ApprovalPolicy(),
        created_at=NOW,
        metadata={"target_refs": {host: host}},
    )
    svc = AttackGraphService(d.graph_store)
    await seed_from_engagement(svc, eng, run_id=RUN, step=0)
    return svc


def _a6_catalog_tool() -> _FixtureToolDescriptor:
    return _FixtureToolDescriptor(
        tool_id=ToolId("recon.probe"),
        server_id="a6-fixture",
        tool_name="probe",
        description="fixture recon probe",
        capability_tags=["recon"],
        operations=["service_discovery"],
        risk_level=_FixtureRisk.LOW,
        health_status=_FixtureHealth.UNKNOWN,
    )


class _A6Catalog:
    def __init__(self, tools) -> None:
        self._tools = {str(t.tool_id): t for t in tools}

    def get(self, tool_id):
        return self._tools.get(str(tool_id))

    def list_by_capability(self, capability_tag: str):
        return [t for t in self._tools.values() if capability_tag in t.capability_tags]

    def list_tools_for_action(self, capability: str, operation: str):
        return [
            t
            for t in self._tools.values()
            if capability in t.capability_tags and operation in (t.operations or [])
        ]


async def _ensure_a6_path(d, *, host: str = "a.local") -> _FixturePathPlan:
    # SAME-run supplied provenance: fresh InternalAssessment only admits
    # declared (engagement scope) targets. Production seeding path, not a mock.
    await _seed_known(d, host)
    existing = await d.path_store.get_active(str(RUN))
    if existing is not None:
        return existing
    plan = _FixturePathPlan(
        path_id=_A6_PATH_ID,
        run_id=str(RUN),
        mission_id=None,
        version=1,
        parent_version=None,
        status="active",
        reason="a6 fixture path",
        goal="probe",
        steps=[
            _FixtureRegisteredStep(
                step_id=_A6_STEP_ID,
                objective="probe host",
                agent_id="recon-agent",
                target_node_ids=[str(_host_node_id_for_fixture(host))],
                principal_refs=[],
                allowed_operations=["recon.service_discovery"],
                depends_on=[],
                required_fact_refs=[],
                unknowns=[],
                completion_criteria=[
                    _FixtureCriterion(kind="evidence", description="probe", min_evidence=1)
                ],
                required=True,
                status="pending",
            )
        ],
        hypothesis_ids=[],
        evidence_ids=[],
        changed_step_ids=[],
        review_records={},
    )
    try:
        return await d.path_store.register(plan)
    except Exception:
        got = await d.path_store.get_active(str(RUN))
        assert got is not None
        return got


def _a6_plan_step(prop) -> PlanStep:
    return PlanStep(
        step_id=_A6_STEP_ID,
        path_id=_A6_PATH_ID,
        path_version=1,
        path_step_id=_A6_STEP_ID,
        capability=prop.capability,
        operation=prop.operation,
        tool_id=ToolId("recon.probe"),
        target=prop.target,
        arguments=dict(prop.arguments),
        reason="probe",
        availability_impact=prop.availability_impact,
    )


class _ScriptedLLM:
    def __init__(self) -> None:
        self._routes: dict[str, list[Any]] = {}
        self.calls: list[dict[str, Any]] = []

    def add(self, route: str, response: Any) -> None:
        self._routes.setdefault(route, []).append(response)

    @staticmethod
    def _route_key(schema: dict[str, Any]) -> str:
        props = schema.get("properties", {})
        if "steps" in props:
            return "path_draft"
        if "decision" in props:
            return "path_review"
        if "mode" in props:
            return "next_action"
        if "criteria" in props:
            return "goal_completion_review"
        if "claims" in props:
            return "task_completion_review"
        if "accepted" in props:
            return "completion_review"
        if "claim_supported" in props:
            return "analysis_verify"
        return "unknown"

    async def complete_structured(self, *, messages, response_schema, system=None, **_: Any):
        route = self._route_key(response_schema)
        self.calls.append({"route": route})
        queue = self._routes.get(route, [])
        assert queue, f"no scripted response left for route {route!r}"
        response = queue.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def complete(self, *, messages, system=None, **_: Any) -> str:
        return ""


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
    # New contract (nodes/observe.py): OBSERVE selects the coordinator
    # identity, not a tool/strategy. Activation lives in AgentDefinition,
    # not in this node. Empty graph still schedules recon-agent to PLAN;
    # only an empty registry goes to REPLAN/NO_AGENTS (guard preserved).
    d = deps()  # Built-in agents exist; empty graph no longer blocks OBSERVE.
    state = await observe_node(initial_graph_state(runtime()), deps=d)

    assert state["runtime"].phase is AgentPhase.PLAN
    assert state["runtime"].current_agent == RECON_AGENT.agent_id
    # The empty-registry guard still holds (see previous test).


# -- PLAN -----------------------------------------------------------------


async def test_plan_states_a_hypothesis_before_it_acts() -> None:
    # New contract: LLM proposes for an accepted path; PLAN compiles and
    # records the hypothesis before the tool ever runs (guard preserved).
    llm = _ScriptedLLM()
    llm.add("next_action", {
        "mode": "actions", "reason": "probe the host",
        "actions": [{
            "path_step_id": _A6_STEP_ID, "agent_id": "recon-agent",
            "target_node_id": "host:a.local", "principal_ref": None,
            "capability": "recon", "operation": "service_discovery",
            "tool_id": "recon.probe", "arguments": {}, "reason": "probe",
            "expected_observation": "probe returns",
            "hypothesis": {"description": "host is reachable", "confidence": 0.4},
            "existing_hypothesis_id": None,
            "expectation": {"kind": "tool_succeeded", "description": "probe ran"},
            "authentication_check": None, "availability_impact": False,
        }],
        "path": None, "path_step_id": None, "completion": {},
        "evidence_request": {}, "hypothesis_update": {},
    })
    d = deps(llm_client=llm, tool_catalog=_A6Catalog([_a6_catalog_tool()]))
    service = await _seed_known(d, "a.local")
    await _ensure_a6_path(d)
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
    assert EventType.ACTION_PROPOSED in d.event_bus.event_types()


async def test_plan_produces_nothing_when_the_frontier_is_empty() -> None:
    d = deps()  # nothing seeded -> no in-scope host
    state = await plan_node(await observe_node(initial_graph_state(runtime()), deps=d), deps=d)

    assert state["action_proposal"] is None
    assert state["runtime"].phase is AgentPhase.REPLAN
    assert route_after_plan(state) == "replan"


async def test_plan_warns_when_an_activated_agent_gets_zero_executable_candidates() -> None:
    # Valid guard: missing tool never executes; explicit LLM failure, no auto
    # fallback to another agent/tool. OBSERVE picks first registered
    # (recon-agent), not web-api-agent.
    llm = _ScriptedLLM()
    llm.add("next_action", {
        "mode": "actions", "reason": "missing tool",
        "actions": [{
            "path_step_id": _A6_STEP_ID, "agent_id": "recon-agent",
            "target_node_id": "host:a.local", "principal_ref": None,
            "capability": "recon", "operation": "service_discovery",
            "tool_id": "recon.missing", "arguments": {}, "reason": "probe",
            "expected_observation": None, "hypothesis": None,
            "existing_hypothesis_id": None, "expectation": None,
            "authentication_check": None, "availability_impact": False,
        }],
        "path": None, "path_step_id": None, "completion": {},
        "evidence_request": {}, "hypothesis_update": {},
    })
    # Repeat hold for budget exhaustion (3 attempts).
    for _ in range(2):
        llm.add("next_action", {
            "mode": "hold", "reason": "no executable",
            "actions": [], "path": None, "path_step_id": None,
            "completion": {}, "evidence_request": {}, "hypothesis_update": {},
        })
    gateway = DummyToolGateway()
    d = deps(
        llm_client=llm,
        tool_catalog=_A6Catalog([_a6_catalog_tool()]),
        tool_gateway=gateway,
    )
    await _seed_known(d, "a.local")
    await _ensure_a6_path(d)
    observed = await observe_node(initial_graph_state(runtime()), deps=d)
    assert observed["runtime"].current_agent == AgentId("recon-agent")
    state = await plan_node(observed, deps=d)
    assert state["action_proposal"] is None
    assert state["runtime"].phase is AgentPhase.REPLAN
    assert gateway.invocations == []


async def test_plan_reports_a_prior_terminal_candidate_once_per_planning_turn() -> None:
    # Valid guard: identical action already recorded never re-executes.
    # New engine compile check reports it as feedback; no duplicate proposal.
    def _nohyp_action():
        return {
            "path_step_id": _A6_STEP_ID, "agent_id": "recon-agent",
            "target_node_id": "host:a.local", "principal_ref": None,
            "capability": "recon", "operation": "service_discovery",
            "tool_id": "recon.probe", "arguments": {}, "reason": "probe",
            "expected_observation": None, "hypothesis": None,
            "existing_hypothesis_id": None, "expectation": None,
            "authentication_check": None, "availability_impact": False,
        }
    llm = _ScriptedLLM()
    llm.add("next_action", {
        "mode": "actions", "reason": "probe", "actions": [_nohyp_action()],
        "path": None, "path_step_id": None, "completion": {},
        "evidence_request": {}, "hypothesis_update": {},
    })
    d = deps(llm_client=llm, tool_catalog=_A6Catalog([_a6_catalog_tool()]))
    await _seed_known(d, "a.local")
    await _ensure_a6_path(d)
    observed = await observe_node(initial_graph_state(runtime()), deps=d)
    planned = await plan_node(observed, deps=d)
    work_id = planned["current_work_id"]
    assert work_id is not None
    assert await d.work_store.complete(
        work_id, owner=str(RUN), status=WorkStatus.SUCCEEDED,
        now=datetime.now(timezone.utc),
    )
    # Same intent again: compile check rejects identical recorded action.
    llm.add("next_action", {
        "mode": "actions", "reason": "probe again", "actions": [_nohyp_action()],
        "path": None, "path_step_id": None, "completion": {},
        "evidence_request": {}, "hypothesis_update": {},
    })
    for _ in range(2):
        llm.add("next_action", {
            "mode": "hold", "reason": "held", "actions": [],
            "path": None, "path_step_id": None, "completion": {},
            "evidence_request": {}, "hypothesis_update": {},
        })
    again = await plan_node(observed, deps=d)
    assert again["action_proposal"] is None
    assert "identical action already recorded" in " ".join(
        again["runtime"].planning_feedback
    ) or again["runtime"].termination_reason in (
        TerminationReason.INVESTIGATION_HELD,
        TerminationReason.PLANNING_ERROR,
    )


async def test_plan_without_a_hypothesis_still_proposes() -> None:
    # New contract: observation action without hypothesis still proposes via
    # explicit scripted LLM for the accepted path; no hypothesis recorded.
    llm = _ScriptedLLM()
    llm.add("next_action", {
        "mode": "actions", "reason": "observe only",
        "actions": [{
            "path_step_id": _A6_STEP_ID, "agent_id": "recon-agent",
            "target_node_id": "host:a.local", "principal_ref": None,
            "capability": "recon", "operation": "service_discovery",
            "tool_id": "recon.probe", "arguments": {}, "reason": "observe",
            "expected_observation": "probe returns",
            "hypothesis": None, "existing_hypothesis_id": None,
            "expectation": None, "authentication_check": None,
            "availability_impact": False,
        }],
        "path": None, "path_step_id": None, "completion": {},
        "evidence_request": {}, "hypothesis_update": {},
    })
    d = deps(llm_client=llm, tool_catalog=_A6Catalog([_a6_catalog_tool()]))
    service = await _seed_known(d, "a.local")
    await _ensure_a6_path(d)
    state = await plan_node(await observe_node(initial_graph_state(runtime()), deps=d), deps=d)
    proposed = state["action_proposal"]
    assert proposed is not None and proposed.hypothesis_id is None
    assert await service.list_hypotheses(status=HYPOTHESIS_OPEN) == []
    assert EventType.ACTION_PROPOSED in d.event_bus.event_types()


async def test_plan_reacquires_a_previously_pending_candidate() -> None:
    # New contract: PLAN enqueues via WorkScheduler and leases PENDING work.
    # Old playbook-compiler candidate_key reacquire is retired; work-ledger
    # lease tracking (PENDING->LEASED, no duplicate execution) is the guard.
    # Compiler diagnostics stay covered in test_orchestration_candidates.py.
    llm = _ScriptedLLM()
    llm.add("next_action", {
        "mode": "actions", "reason": "probe",
        "actions": [{
            "path_step_id": _A6_STEP_ID, "agent_id": "recon-agent",
            "target_node_id": "host:a.local", "principal_ref": None,
            "capability": "recon", "operation": "service_discovery",
            "tool_id": "recon.probe", "arguments": {}, "reason": "probe",
            "expected_observation": None, "hypothesis": None,
            "existing_hypothesis_id": None, "expectation": None,
            "authentication_check": None, "availability_impact": False,
        }],
        "path": None, "path_step_id": None, "completion": {},
        "evidence_request": {}, "hypothesis_update": {},
    })
    d = deps(llm_client=llm, tool_catalog=_A6Catalog([_a6_catalog_tool()]))
    await _seed_known(d, "a.local")
    await _ensure_a6_path(d)
    observed = await observe_node(initial_graph_state(runtime()), deps=d)
    planned = await plan_node(observed, deps=d)
    assert planned["current_work_id"] is not None
    restored = await d.work_store.get(planned["current_work_id"])
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
    await _ensure_a6_path(d)
    await seeded(d, "a.local")
    prop = proposal()
    state = state_with(runtime(), action_proposal=prop, plan_step=_a6_plan_step(prop))

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
    await _ensure_a6_path(d)
    await seeded(d, "a.local")
    prop = proposal()
    state = state_with(runtime(), action_proposal=prop, plan_step=_a6_plan_step(prop))
    await policy_node(state, deps=d)

    assert len(d.event_bus.correlated("action_1")) == 2


# -- EXECUTE --------------------------------------------------------------


async def test_execute_uses_the_tool_the_chosen_step_declared() -> None:
    gateway = DummyToolGateway()
    d = deps(tool_gateway=gateway)
    await _ensure_a6_path(d)
    await seeded(d, "a.local")
    prop = proposal()
    state = state_with(
        runtime(remaining_budget=3),
        action_proposal=prop,
        plan_step=_a6_plan_step(prop),
    )
    state = await policy_node(state, deps=d)

    result = await execute_node(state, deps=d)

    assert gateway.invocations[0].tool_id == ToolId("recon.probe")
    assert result["runtime"].actions_executed == 1
    assert result["runtime"].remaining_budget == 2
    assert result["runtime"].consecutive_failures == 0
    assert EventType.TOOL_EXECUTION_FINISHED in d.event_bus.event_types()


async def test_execute_counts_consecutive_failures() -> None:
    d = deps(tool_gateway=DummyToolGateway(success=False))
    await _ensure_a6_path(d)
    await seeded(d, "a.local")
    prop = proposal()
    state = state_with(
        runtime(consecutive_failures=1),
        action_proposal=prop,
        plan_step=_a6_plan_step(prop),
    )
    state = await policy_node(state, deps=d)

    result = await execute_node(state, deps=d)

    assert result["runtime"].consecutive_failures == 2
    assert EventType.TOOL_EXECUTION_FAILED in d.event_bus.event_types()


async def test_execute_does_not_invoke_tool_after_lease_is_lost() -> None:
    gateway = DummyToolGateway()
    d = deps(tool_gateway=gateway)
    await _ensure_a6_path(d)
    await seeded(d, "a.local")
    prop = proposal()
    state = state_with(
        runtime(remaining_budget=3),
        action_proposal=prop,
        plan_step=_a6_plan_step(prop),
        current_work_id="missing-work",
    )
    state = await policy_node(state, deps=d)

    result = await execute_node(state, deps=d)

    assert gateway.invocations == []
    tool = result["tool_result"]
    assert tool is not None and tool.success is False
    assert tool.error is not None and tool.error.code == "lease_lost_before_execution"
    assert tool.metadata["orchestration_exception"] is True


async def test_execute_rejects_a_tool_binding_for_a_different_approved_intent() -> None:
    gateway = DummyToolGateway()
    d = deps(tool_gateway=gateway)
    await _ensure_a6_path(d)
    await seeded(d, "a.local")
    prop = proposal()
    state = state_with(runtime(), action_proposal=prop, plan_step=_a6_plan_step(prop))
    state = await policy_node(state, deps=d)
    # Same accepted path/step envelope, different approved intent: the step
    # range guard (operation left the envelope) must refuse the binding.
    mismatched = _a6_plan_step(prop).model_copy(update={"operation": "endpoint_discovery"})
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
    # New contract (analysis.hypothesis_status_for_assessment): grounded
    # success confirms the tested claim, including a benign PASS. A benign
    # confirmation never becomes a Finding (promotion still needs VIOLATION).
    assert await hypothesis_status(service, "hyp_1") == HYPOTHESIS_CONFIRMED
    assert d.event_bus.event_types() == [EventType.EVIDENCE_CREATED, HYPOTHESIS_RESOLVED]
    assert (
        d.event_bus.history(event_type=HYPOTHESIS_RESOLVED)[0].payload["status"]
        == HYPOTHESIS_CONFIRMED
    )


async def test_a_failed_execution_without_a_follow_up_closes_the_hypothesis() -> None:
    # New contract: a failed tool call maps to UNKNOWN/inconclusive with the
    # failure code as the traceable reason. hypothesis_status_for_assessment
    # never returns OPEN for outcome=unknown, so the old R7
    # no_follow_up_verification_available branch no longer fires here.
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
    reason = d.event_bus.history(event_type=HYPOTHESIS_RESOLVED)[0].payload["reason"]
    assert "timeout" in reason and "no verdict can be reached" in reason


async def test_a_failed_execution_keeps_a_repeatable_hypothesis_open() -> None:
    # New contract: outcome=unknown always maps to inconclusive, even for a
    # repeatable step. OPEN is reserved for a verification that can still run
    # with a grounded open outcome, not for a failed execution. Both
    # repeatable and non-repeatable failures close inconclusive here.
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

    assert await hypothesis_status(service, "hyp_1") == HYPOTHESIS_INCONCLUSIVE
    assert (
        d.event_bus.history(event_type=HYPOTHESIS_RESOLVED)[0].payload["status"]
        == HYPOTHESIS_INCONCLUSIVE
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
    # New contract (nodes/update.py): planned attrs are never promoted into
    # measured facts. Only result mappers write domain facts; a bare success
    # cannot certify reachable/probed. The attempt is still recorded.
    assert attrs_of(node.properties) == {}
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
    # New contract: the HOST write carries no planned attrs (see above);
    # SERVICE/ENDPOINT discovery still lands via the recon.http_probe mapper.
    assert attrs_of(host_node.properties) == {}

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

    # Case E: one GRAPH_UPDATED for the host attempt plus one per discovered
    # node, plus the GraphMutationApplied summary for that batch.
    assert d.event_bus.event_types() == [EventType.GRAPH_UPDATED] * 3 + [
        GRAPH_MUTATION_APPLIED
    ]


async def test_update_records_manifest_findings_with_evidence_and_graph_relations() -> None:
    d = deps()
    service = AttackGraphService(d.graph_store)
    target = AssetTarget(asset_id=AssetId("insecurebankv2"))
    manifest = """<manifest xmlns:android="http://schemas.android.com/apk/res/android"
      package="com.example.insecure">
      <uses-permission android:name="android.permission.SEND_SMS" />
      <application android:debuggable="true" android:usesCleartextTraffic="true">
        <activity android:name=".Exported" android:exported="true" />
      </application>
    </manifest>"""
    analysis_proposal = ActionProposal(
        action_id=ActionId("action_manifest"),
        run_id=RUN,
        engagement_id=ENGAGEMENT,
        agent_id=AgentId("analysis-agent"),
        capability="analysis",
        operation="static_analysis",
        target=target,
        reason="inspect manifest",
        created_at=NOW,
    )
    state = state_with(
        runtime(step_count=2),
        action_proposal=analysis_proposal,
        tool_result=tool_result(
            output={"success": True, "manifest": manifest},
            tool_id="analysis.get_manifest",
        ),
        plan_step=ANALYSIS_AGENT.playbook[1],
        result_evidence_id=EvidenceId("ev_manifest"),
    )

    await update_node(state, deps=d)

    findings = await service.list_findings(verified=True)
    assert len(findings) == 4
    assert all(finding.evidence_ids == ["ev_manifest"] for finding in findings)
    assert {finding.metadata["verdict"] for finding in findings} == {
        Verdict.VIOLATION.value
    }
    hypotheses = await service.list_hypotheses(status=HYPOTHESIS_CONFIRMED)
    assert len(hypotheses) == 4
    edges = await service.list_edges()
    assert sum(edge.type is GraphEdgeType.TARGETS for edge in edges) == 8
    assert sum(edge.type is GraphEdgeType.SUPPORTED_BY for edge in edges) == 4
    assert len(d.event_bus.history(event_type=EventType.FINDING_CREATED)) == 4
    assert len(d.event_bus.history(event_type=EventType.FINDING_VERIFIED)) == 4

    await update_node(merged(state, runtime=runtime(step_count=3)), deps=d)

    assert len(await service.list_findings(verified=True)) == 4
    assert len(await service.list_hypotheses(status=HYPOTHESIS_CONFIRMED)) == 4
    assert len(await service.list_edges()) == 12
    assert len(d.event_bus.history(event_type=EventType.FINDING_CREATED)) == 4
    assert len(d.event_bus.history(event_type=EventType.FINDING_VERIFIED)) == 4


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
    endpoints = [
        node for node in await graph.list_nodes() if node.type is GraphNodeType.ENDPOINT
    ]
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
    # New contract (nodes/replan.py): REPLAN owns limits/termination only;
    # agent retirement via finished_agents is gone. A turn with no reason
    # continues to OBSERVE and always emits one REPLAN_TRIGGERED (continue).
    # Alternative coverage for scheduling/activation lives in
    # tests/unit/test_agents.py (registry next_agent/activation).
    d = deps()
    state = initial_graph_state(runtime(step_count=1, current_agent=RECON_AGENT.agent_id))

    result = await replan_node(state, deps=d)

    assert result["runtime"].phase is AgentPhase.OBSERVE
    assert result["runtime"].termination_reason is None
    triggered = d.event_bus.history(event_type=REPLAN_TRIGGERED)
    assert len(triggered) == 1 and triggered[0].payload["outcome"] == "continue"


async def test_replan_ends_the_run_when_every_agent_is_finished() -> None:
    # New contract: no finished_agents terminal branch. Termination comes
    # from explicit reasons/hard stops only; a bare turn continues.
    d = deps()
    state = initial_graph_state(runtime(step_count=1, current_agent=RECON_AGENT.agent_id))

    result = await replan_node(state, deps=d)

    assert result["runtime"].phase is AgentPhase.OBSERVE
    assert route_after_replan(result) == "observe"
    triggered = d.event_bus.history(event_type=REPLAN_TRIGGERED)
    assert triggered[0].payload["outcome"] == "continue"


@pytest.mark.parametrize(
    ("state_kwargs", "settings", "expected"),
    [
        (
            {"step_count": 4},
            OrchestrationSettings(max_steps=4),
            TerminationReason.MAX_STEPS_REACHED,
        ),
        # NOTE: budget=0 no longer terminates in REPLAN; enforcement moved to
        # PLAN (BUDGET_EXHAUSTED when batch capacity < 1) and Control.
        # Alternative coverage: plan BUDGET_EXHAUSTED in
        # tests/integration/test_engine_llm_investigation_flow.py::
        # test_budget_zero_allows_completion_review_but_blocks_new_execution
        # and tests/unit/test_budget.py.
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

    # New contract: CONSECUTIVE_FAILURES is FAILED (not FINISHED); MAX_STEPS
    # stays FINISHED. The phase distinction is the guard.
    if expected == TerminationReason.CONSECUTIVE_FAILURES:
        assert result["runtime"].phase is AgentPhase.FAILED
    else:
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
    # Valid guard: never guess $path / out-of-envelope target never executes.
    # New engine reports it as compile feedback (held), not the retired
    # CANDIDATE_REJECTED playbook event. Placeholder/drop diagnostics stay
    # covered in test_orchestration_candidates.py.
    llm = _ScriptedLLM()
    llm.add("next_action", {
        "mode": "actions", "reason": "bad target",
        "actions": [{
            "path_step_id": _A6_STEP_ID, "agent_id": "recon-agent",
            "target_node_id": "service:a.local/http", "principal_ref": None,
            "capability": "recon", "operation": "service_discovery",
            "tool_id": "recon.probe", "arguments": {}, "reason": "probe",
            "expected_observation": None, "hypothesis": None,
            "existing_hypothesis_id": None, "expectation": None,
            "authentication_check": None, "availability_impact": False,
        }],
        "path": None, "path_step_id": None, "completion": {},
        "evidence_request": {}, "hypothesis_update": {},
    })
    for _ in range(2):
        llm.add("next_action", {
            "mode": "hold", "reason": "held", "actions": [],
            "path": None, "path_step_id": None, "completion": {},
            "evidence_request": {}, "hypothesis_update": {},
        })
    d = deps(llm_client=llm, tool_catalog=_A6Catalog([_a6_catalog_tool()]))
    await _seed_known(d, "a.local")
    await _ensure_a6_path(d)
    state = await observe_node(initial_graph_state(runtime()), deps=d)
    planned = await plan_node(state, deps=d)
    assert planned["action_proposal"] is None
    assert planned["runtime"].phase is AgentPhase.REPLAN
    assert planned["runtime"].termination_reason == TerminationReason.INVESTIGATION_HELD


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
    # Valid guard: operation outside step envelope never executes.
    # Retired AGENT_NO_EXECUTABLE playbook event stays covered in
    # test_orchestration_candidates.py; here the engine feedback is the guard.
    llm = _ScriptedLLM()
    llm.add("next_action", {
        "mode": "actions", "reason": "bad operation",
        "actions": [{
            "path_step_id": _A6_STEP_ID, "agent_id": "recon-agent",
            "target_node_id": "host:a.local", "principal_ref": None,
            "capability": "recon", "operation": "endpoint_discovery",
            "tool_id": "recon.probe", "arguments": {}, "reason": "probe",
            "expected_observation": None, "hypothesis": None,
            "existing_hypothesis_id": None, "expectation": None,
            "authentication_check": None, "availability_impact": False,
        }],
        "path": None, "path_step_id": None, "completion": {},
        "evidence_request": {}, "hypothesis_update": {},
    })
    for _ in range(2):
        llm.add("next_action", {
            "mode": "hold", "reason": "held", "actions": [],
            "path": None, "path_step_id": None, "completion": {},
            "evidence_request": {}, "hypothesis_update": {},
        })
    gateway = DummyToolGateway()
    d = deps(
        llm_client=llm,
        tool_catalog=_A6Catalog([_a6_catalog_tool()]),
        tool_gateway=gateway,
    )
    await _seed_known(d, "a.local")
    await _ensure_a6_path(d)
    state = await observe_node(initial_graph_state(runtime()), deps=d)
    planned = await plan_node(state, deps=d)
    assert planned["action_proposal"] is None
    assert planned["runtime"].phase is AgentPhase.REPLAN
    assert gateway.invocations == []


async def test_a_diagnostic_only_provider_is_reported_as_no_executable_candidates() -> None:
    # Valid guard: diagnostic-only / hold never executes.
    # Provider diagnostics stay covered in test_orchestration_candidates.py
    # and provider unit tests; here hold-without-actions is the guard.
    llm = _ScriptedLLM()
    llm.add("next_action", {
        "mode": "hold", "reason": "diagnostic only: no executable",
        "actions": [], "path": None, "path_step_id": None,
        "completion": {}, "evidence_request": {}, "hypothesis_update": {},
    })
    gateway = DummyToolGateway()
    d = deps(
        llm_client=llm,
        tool_catalog=_A6Catalog([_a6_catalog_tool()]),
        tool_gateway=gateway,
    )
    await _seed_known(d, "a.local")
    await _ensure_a6_path(d)
    state = await observe_node(initial_graph_state(runtime()), deps=d)
    planned = await plan_node(state, deps=d)
    assert planned["action_proposal"] is None
    assert planned["runtime"].phase is AgentPhase.REPLAN
    assert planned["runtime"].termination_reason == TerminationReason.INVESTIGATION_HELD
    assert gateway.invocations == []


async def test_a_provider_with_nothing_to_say_stays_silent() -> None:
    # "Nothing to do" is genuinely different from "something was considered
    # and none of it was executable", and must not start emitting events.
    from redteam.orchestration.agents.providers import CandidateBatch
    from redteam.orchestration.events import AGENT_NO_EXECUTABLE_CANDIDATES

    d = deps(
        candidate_providers=_providers_with_web(
            _ProviderStub(CandidateBatch(candidates=[]))
        )
    )
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
    # New contract: a bare turn is continue/review_results, not terminate.
    d = deps()
    state = initial_graph_state(runtime(step_count=1, current_agent=RECON_AGENT.agent_id))

    await replan_node(state, deps=d)

    triggered = d.event_bus.history(event_type=REPLAN_TRIGGERED)
    assert len(triggered) == 1
    assert triggered[0].payload["outcome"] == "continue"
    assert triggered[0].payload["reason"] == "review_results"


async def test_replan_stays_silent_when_it_is_only_taking_another_turn() -> None:
    # New contract: REPLAN always emits one REPLAN_TRIGGERED per turn.
    d = deps()
    state = state_with(runtime(step_count=1), action_proposal=proposal())

    result = await replan_node(state, deps=d)

    triggered = d.event_bus.history(event_type=REPLAN_TRIGGERED)
    assert len(triggered) == 1
    assert triggered[0].payload["outcome"] == "continue"
    assert triggered[0].payload["reason"] == "review_results"
    assert result["runtime"].phase is AgentPhase.OBSERVE


async def test_replan_maps_a_hard_stop_to_its_own_reason() -> None:
    d = deps(settings=OrchestrationSettings(max_steps=4))
    state = state_with(
        runtime(step_count=4, current_agent=RECON_AGENT.agent_id), action_proposal=proposal()
    )

    await replan_node(state, deps=d)

    triggered = d.event_bus.history(event_type=REPLAN_TRIGGERED)
    assert [item.payload["reason"] for item in triggered] == ["max_steps_reached"]
