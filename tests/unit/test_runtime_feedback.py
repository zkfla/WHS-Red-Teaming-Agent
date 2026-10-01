"""Structured execution feedback across the single-action and batch paths."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, cast

from redteam.attack_graph.identity import host_node_id
from redteam.attack_graph.predicates import attrs_of
from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
)
from redteam.core.contracts.policy import PolicyDecision
from redteam.core.contracts.target import HostTarget
from redteam.core.contracts.tool import ToolError, ToolResult
from redteam.core.enums import AgentPhase, GraphNodeType, PolicyVerdict
from redteam.core.ids import (
    ActionId,
    AgentId,
    ApprovalId,
    EngagementId,
    GraphNodeId,
    InvocationId,
    RunId,
    ToolId,
)
from redteam.event_bus.memory import InMemoryEventBus
from redteam.orchestration.agents import PlanStep
from redteam.orchestration.agents.registry import RECON_AGENT
from redteam.orchestration.candidates.models import PriorityScore
from redteam.orchestration.dispatch import (
    DispatchItem,
    WorkerOutcome,
    merge_worker_outcomes,
)
from redteam.orchestration.nodes import aggregate_node, execute_node, policy_node, update_node
from redteam.orchestration.nodes.worker import worker_node
from redteam.orchestration.paths import PathPlan, PathStep
from redteam.orchestration.runtime_observation import RuntimeObservation, append_runtime_observation
from redteam.orchestration.seed import seed_from_engagement
from redteam.orchestration.state import GraphState, RuntimeState, initial_graph_state
from redteam.orchestration.work.models import WorkItem
from tests.fakes import DummyToolGateway, FakeControlPlane, build_test_application

NOW = datetime.now(timezone.utc)
RUN = RunId("run_feedback_2")
ENGAGEMENT = EngagementId("eng_feedback_2")


def runtime(**overrides):
    return RuntimeState(run_id=RUN, engagement_id=ENGAGEMENT, **overrides)


def deps(**overrides: Any):
    overrides.setdefault("event_bus", InMemoryEventBus())
    return build_test_application(**overrides)


def state_with(rt=None, **transient):
    base = dict(initial_graph_state(rt if rt is not None else runtime()))
    base.update(transient)
    return cast(GraphState, base)


def proposal(action="action_1", host="a.local"):
    return ActionProposal(
        action_id=ActionId(action),
        run_id=RUN,
        engagement_id=ENGAGEMENT,
        agent_id=AgentId("recon-agent"),
        capability="recon",
        operation="service_discovery",
        target=HostTarget(host=host),
        reason="probe",
        expected_observation="echo comes back",
        created_at=NOW,
    )


def plan_step(prop):
    return PlanStep(
        step_id="probe-host",
        path_id="path-feedback",
        path_version=1,
        path_step_id="probe-host",
        capability=prop.capability,
        operation=prop.operation,
        tool_id=ToolId("recon.probe"),
        target=prop.target,
        arguments=dict(prop.arguments),
        reason="probe",
        availability_impact=prop.availability_impact,
    )


def tool_result(success=True):
    return ToolResult(
        invocation_id=InvocationId("inv_1"),
        action_id=ActionId("action_1"),
        tool_id=ToolId("recon.probe"),
        success=success,
        output={"echo": {"message": "hi"}},
        error=None if success else ToolError(code="timeout", message="no answer"),
        started_at=NOW,
        finished_at=NOW,
    )


async def _seed_host(d, host="a.local"):
    eng = Engagement(
        engagement_id=ENGAGEMENT,
        name="feedback fixture",
        scope=EngagementScope(allowed_hosts=[host]),
        rules_of_engagement=[],
        execution_budget=ExecutionBudget(max_actions=10),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=30, max_concurrent_actions=2),
        approval_policy=ApprovalPolicy(),
        created_at=NOW,
        metadata={"target_refs": {host: host}},
    )
    service = AttackGraphService(d.graph_store)
    await seed_from_engagement(service, eng, run_id=RUN, step=0)
    await service.upsert_node(GraphNodeType.HOST, key=host, attrs={"in_scope": True})
    return service


async def _ensure_path(d, host="a.local"):
    existing = await d.path_store.get_active(str(RUN))
    if existing is not None:
        return existing
    plan = PathPlan(
        path_id="path-feedback",
        run_id=str(RUN),
        version=1,
        status="active",
        reason="feedback fixture path",
        goal="probe",
        steps=[
            PathStep(
                step_id="probe-host",
                objective="probe host",
                target_node_ids=[str(host_node_id(host))],
                allowed_operations=["recon.service_discovery"],
            )
        ],
    )
    try:
        return await d.path_store.register(plan)
    except Exception:
        got = await d.path_store.get_active(str(RUN))
        assert got is not None
        return got


async def test_policy_allow_records_control_observation() -> None:
    d = deps()
    await _ensure_path(d)
    await _seed_host(d)
    prop = proposal()
    state = state_with(runtime(), action_proposal=prop, plan_step=plan_step(prop))
    result = await policy_node(state, deps=d)
    assert result["runtime"].phase is AgentPhase.EXECUTE
    window = result["runtime"].recent_observations
    assert len(window) == 1
    entry = window[0]
    assert entry.kind == "control"
    assert entry.outcome == "allow"
    assert entry.action_id == "action_1"
    assert entry.capability == "recon"
    assert "FakeControlPlane" in entry.reasons


async def test_policy_early_rejection_records_control_observation() -> None:
    d = deps()
    await _seed_host(d)
    prop = proposal()
    state = state_with(runtime(), action_proposal=prop, plan_step=plan_step(prop))
    result = await policy_node(state, deps=d)
    assert result["runtime"].phase is AgentPhase.REPLAN
    assert result["runtime"].planning_feedback != []
    window = result["runtime"].recent_observations
    assert len(window) == 1
    entry = window[0]
    assert entry.kind == "control"
    assert entry.outcome == "deny"
    assert entry.action_id == "action_1"
    assert "orchestration:stale_or_invalid_path" in entry.matched_rules


async def test_policy_approval_wait_then_resolved_allow_keeps_old_reference() -> None:
    plane = FakeControlPlane(default_verdict=PolicyVerdict.REQUIRE_APPROVAL)
    d = deps(control_plane=plane)
    await _ensure_path(d)
    await _seed_host(d)
    prop = proposal()
    state = state_with(runtime(), action_proposal=prop, plan_step=plan_step(prop))
    waited = await policy_node(state, deps=d)
    pending_id = waited["runtime"].pending_approval_id
    assert pending_id is not None
    first = waited["runtime"].recent_observations[-1]
    assert first.outcome == "require_approval"
    assert first.requires_approval is True
    assert first.approval_id == str(pending_id)
    plane.resolve_approval(
        pending_id,
        PolicyDecision(
            action_id=prop.action_id,
            verdict=PolicyVerdict.ALLOW,
            reasons=["human allowed"],
            evaluated_at=NOW,
        ),
    )
    resumed_runtime = waited["runtime"].model_copy(update={"pending_action_id": prop.action_id})
    resumed = await policy_node(
        state_with(resumed_runtime, action_proposal=prop, plan_step=plan_step(prop)), deps=d
    )
    assert resumed["runtime"].phase is AgentPhase.EXECUTE
    last = resumed["runtime"].recent_observations[-1]
    assert last.outcome == "allow"
    assert last.action_id == "action_1"
    assert last.approval_id == str(pending_id)


async def test_execute_success_and_failure_record_tool_observations() -> None:
    for success, outcome in ((True, "success"), (False, "failed")):
        d = deps(tool_gateway=DummyToolGateway(success=success))
        await _ensure_path(d)
        await _seed_host(d)
        prop = proposal()
        state = state_with(runtime(), action_proposal=prop, plan_step=plan_step(prop))
        state = await policy_node(state, deps=d)
        result = await execute_node(state, deps=d)
        tool_entries = [o for o in result["runtime"].recent_observations if o.kind == "tool"]
        assert len(tool_entries) == 1
        assert tool_entries[0].outcome == outcome
        assert tool_entries[0].action_id == "action_1"
        if not success:
            assert "dummy_failure" in tool_entries[0].reasons


async def test_execute_lease_loss_still_records_failure_observation() -> None:
    d = deps(tool_gateway=DummyToolGateway())
    await _ensure_path(d)
    await _seed_host(d)
    prop = proposal()
    state = state_with(
        runtime(), action_proposal=prop, plan_step=plan_step(prop), current_work_id="missing-work"
    )
    state = await policy_node(state, deps=d)
    result = await execute_node(state, deps=d)
    tool_entries = [o for o in result["runtime"].recent_observations if o.kind == "tool"]
    assert len(tool_entries) == 1
    assert tool_entries[0].outcome == "failed"
    assert "lease_lost_before_execution" in tool_entries[0].reasons


async def test_update_records_state_observation_without_promoting_plan() -> None:
    d = deps()
    service = AttackGraphService(d.graph_store)
    state = state_with(
        runtime(step_count=1),
        action_proposal=proposal(),
        tool_result=tool_result(),
        plan_step=RECON_AGENT.playbook[0],
    )
    result = await update_node(state, deps=d)
    window = result["runtime"].recent_observations
    assert len(window) == 1
    entry = window[0]
    assert entry.kind == "state"
    assert entry.outcome == "updated"
    assert entry.action_id == "action_1"
    node = await service.get_node(GraphNodeId("host:a.local"))
    assert node is not None
    assert attrs_of(node.properties) == {}
    assert entry.target == str(node.id)


def _fanout_step(action, host):
    return PlanStep(
        step_id=action,
        capability="recon",
        operation="service_discovery",
        tool_id=ToolId("recon.probe"),
        target=HostTarget(host=host),
        reason="fan-out",
    )


def _fanout_proposal(action, host):
    return ActionProposal(
        action_id=ActionId(action),
        run_id=RUN,
        engagement_id=ENGAGEMENT,
        agent_id=AgentId("recon-agent"),
        capability="recon",
        operation="service_discovery",
        target=HostTarget(host=host),
        reason="fan-out",
        created_at=NOW,
    )


def _fanout_item(action, host):
    return DispatchItem(
        work_id="work_" + action,
        plan_step=_fanout_step(action, host),
        proposal=_fanout_proposal(action, host),
    )


async def _register_fanout_path(d, items):
    steps = []
    for index, item in enumerate(items):
        host = item.proposal.target.host
        steps.append(
            PathStep(
                step_id="pstep_" + str(index),
                objective="probe " + host,
                target_node_ids=["host:" + host],
                allowed_operations=["recon.service_discovery"],
            )
        )
    plan = PathPlan(
        path_id="path-fanout-feedback",
        run_id=str(items[0].proposal.run_id),
        version=1,
        status="active",
        reason="fanout feedback path",
        goal="fanout",
        steps=steps,
    )
    await d.path_store.register(plan)
    bound = []
    for index, item in enumerate(items):
        bound.append(
            item.model_copy(
                update={
                    "plan_step": item.plan_step.model_copy(
                        update={
                            "path_id": "path-fanout-feedback",
                            "path_version": 1,
                            "path_step_id": "pstep_" + str(index),
                        }
                    )
                }
            )
        )
    return bound


async def _lease_item(d, item):
    now = datetime.now(timezone.utc)
    await d.work_store.add(
        WorkItem(
            work_id=item.work_id,
            run_id=item.proposal.run_id,
            candidate_key=item.work_id,
            agent_id=item.proposal.agent_id,
            target_node_ids=[GraphNodeId("host:" + item.proposal.target.host)],
            priority=PriorityScore(total=1),
            created_at=now,
            updated_at=now,
        )
    )
    await d.work_store.acquire(
        item.work_id, owner=str(item.proposal.run_id), now=now, ttl=timedelta(seconds=60)
    )


async def test_worker_carries_only_own_action_observations() -> None:
    plane = FakeControlPlane(default_verdict=PolicyVerdict.REQUIRE_APPROVAL)
    d = deps(control_plane=plane)
    foreign = RuntimeObservation(
        kind="tool",
        summary="older action done",
        outcome="success",
        step=0,
        action_id="action_older",
        capability="recon",
        operation="service_discovery",
    )
    base = append_runtime_observation(runtime(), foreign)
    assert len(base.recent_observations) == 1
    raw = _fanout_item("action_mine", "a.local")
    bound = await _register_fanout_path(d, [raw])
    await _lease_item(d, bound[0])
    await _seed_host(d)
    result = await worker_node({**state_with(base), "dispatch_item": bound[0]}, deps=d)
    outcome = result["worker_outcomes"]["action_mine"]
    assert len(outcome.runtime_observations) == 2
    assert all(o.action_id == "action_mine" for o in outcome.runtime_observations)
    assert [o.kind for o in outcome.runtime_observations] == ["control", "state"]
    assert outcome.runtime_observations[0].outcome == "require_approval"
    assert outcome.runtime_observations[0].requires_approval is True
    assert outcome.runtime_observations[0].approval_id is not None


async def test_aggregate_folds_worker_records_in_dispatch_order() -> None:
    d = deps()
    first = _fanout_item("action_first", "a.local")
    second = _fanout_item("action_second", "b.local")
    carried_first = [
        RuntimeObservation(
            kind="control",
            summary="Control ALLOW for recon.service_discovery",
            outcome="allow",
            step=0,
            action_id="action_first",
            capability="recon",
            operation="service_discovery",
        ),
        RuntimeObservation(
            kind="tool",
            summary="Tool recon.probe succeeded",
            outcome="success",
            step=0,
            action_id="action_first",
            capability="recon",
            operation="service_discovery",
        ),
    ]
    carried_second = [
        RuntimeObservation(
            kind="control",
            summary="Control DENY for recon.service_discovery: blocked",
            outcome="deny",
            step=0,
            action_id="action_second",
            capability="recon",
            operation="service_discovery",
            reasons=["blocked"],
        ),
    ]
    outcomes = {
        "action_first": WorkerOutcome(
            action_id="action_first",
            work_id="work_action_first",
            executed=True,
            succeeded=True,
            runtime_observations=carried_first,
        ),
        "action_second": WorkerOutcome(
            action_id="action_second",
            work_id="work_action_second",
            executed=False,
            policy_verdict=PolicyVerdict.DENY,
            approval_id=ApprovalId("appr_x"),
            runtime_observations=carried_second,
        ),
    }
    state = state_with(
        runtime(remaining_budget=5), dispatch_items=[first, second], worker_outcomes=outcomes
    )
    result = await aggregate_node(state, deps=d)
    window = result["runtime"].recent_observations
    assert [(o.action_id, o.kind) for o in window] == [
        ("action_first", "control"),
        ("action_first", "tool"),
        ("action_second", "control"),
    ]
    assert result["runtime"].actions_executed == 1
    assert result["runtime"].pending_action_id == "action_second"
    assert result["runtime"].pending_approval_id == "appr_x"
    from langgraph.types import Overwrite

    cleared = result["worker_outcomes"]
    assert isinstance(cleared, Overwrite)
    assert cleared.value == {}


def test_worker_outcome_merge_and_old_results_stay_valid() -> None:
    fresh = WorkerOutcome(action_id="action_1", work_id="work_1")
    assert fresh.runtime_observations == []
    legacy = WorkerOutcome.model_validate({"action_id": "action_1", "work_id": "work_1"})
    assert legacy.runtime_observations == []
    merged = merge_worker_outcomes({"a": fresh}, {"a": legacy, "b": fresh})
    assert set(merged.keys()) == {"a", "b"}
