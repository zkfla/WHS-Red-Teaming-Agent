"""End-to-End Test: Agent -> Control Plane -> Capability -> Evidence -> Event -> Attack Graph.

Exercises the Minimal Vertical Slice from instruction.md section 16
using every real skeleton component except the LLM (FakeLLMClient) and
the MCP server (ExampleMCPAdapter, simulated in-process).

Two expectations changed with the real re-plan loop (see
docs/agent/orchestration.md): ``HypothesisCreated``/``AgentFinished`` are
now emitted, and node ids follow ``<type>:<key>``. The assertions below are
stronger than before, not looser.
"""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.app.bootstrap import build_runtime_deps, run_engagement
from redteam.attack_graph.predicates import attrs_of, facts_of
from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
)
from redteam.core.contracts.events import EventEnvelope, EventType
from redteam.core.enums import AgentPhase, Verdict
from redteam.core.ids import AgentId, EngagementId, GraphNodeId
from redteam.orchestration.events import (
    CANDIDATE_GENERATED,
    HYPOTHESIS_RESOLVED,
    REPLAN_TRIGGERED,
    REPORT_PROJECTED,
)
from redteam.orchestration.state import TerminationReason


def _engagement() -> Engagement:
    return Engagement(
        engagement_id=EngagementId("eng_e2e"),
        name="e2e",
        scope=EngagementScope(allowed_hosts=["example.local"]),
        execution_budget=ExecutionBudget(max_actions=5),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=30),
        approval_policy=ApprovalPolicy(),
        created_at=datetime.now(timezone.utc),
    )


async def test_minimal_vertical_slice_end_to_end(tmp_path) -> None:
    engagement = _engagement()
    deps = await build_runtime_deps(engagement, evidence_dir=tmp_path)

    observed_events: list[EventEnvelope] = []

    async def _collect(event: EventEnvelope) -> None:
        observed_events.append(event)

    await deps.event_bus.subscribe("*", _collect)

    final_state = await run_engagement(engagement, deps=deps)

    # 13. Run ends in a terminal phase, with a verdict and a named reason.
    assert final_state.phase == AgentPhase.FINISHED
    assert final_state.last_verdict == Verdict.PASS
    assert final_state.termination_reason == TerminationReason.NO_ACTIONABLE_PLAN
    assert final_state.actions_executed == 1
    assert final_state.finished_agents == [AgentId("recon-agent")]

    # 3-11. The full chain of events fired, in order, all tagged with
    # the right run/engagement.
    expected_in_order = [
        EventType.RUN_STARTED,
        EventType.AGENT_STARTED,
        CANDIDATE_GENERATED,
        EventType.HYPOTHESIS_CREATED,
        EventType.ACTION_PROPOSED,
        EventType.POLICY_EVALUATED,
        EventType.ACTION_APPROVED,
        EventType.TOOL_EXECUTION_STARTED,
        EventType.TOOL_EXECUTION_FINISHED,
        EventType.EVIDENCE_CREATED,
        HYPOTHESIS_RESOLVED,
        EventType.GRAPH_UPDATED,
        EventType.AGENT_FINISHED,
        REPLAN_TRIGGERED,
        REPORT_PROJECTED,
        EventType.RUN_FINISHED,
    ]
    assert [e.event_type for e in observed_events] == expected_in_order
    assert all(e.run_id == final_state.run_id for e in observed_events)
    assert all(e.engagement_id == engagement.engagement_id for e in observed_events)

    # 10. Everything from the proposal onwards shares one correlation id,
    # so the action can be reconstructed from the event log alone.
    action_id = next(
        e.payload["action_id"]
        for e in observed_events
        if e.event_type == EventType.ACTION_PROPOSED
    )
    correlated = [e.event_type for e in observed_events if e.correlation_id == action_id]
    # From the hypothesis the action was raised for through the update that
    # settled it -- CandidateGenerated is not part of this action's chain
    # (it summarises the whole compiled frontier, before a step is chosen).
    assert correlated == expected_in_order[3:12]

    # 9. Evidence was recorded for the run, correlated to the action.
    evidence = await deps.evidence_store.list_for_run(final_state.run_id)
    assert len(evidence) == final_state.actions_executed
    assert evidence[0].action_id == action_id
    assert evidence[0].invocation_id is not None
    assert evidence[0].engagement_id == engagement.engagement_id

    # 11. Attack Graph was updated with the target, under its stable id.
    node = await deps.graph_store.get_node(GraphNodeId("host:example.local"))
    assert node is not None
    assert node.type.value == "host"
    assert attrs_of(node.properties)["reachable"] is True
    # ...and the attempt is recorded, which is what stops the planner
    # proposing the same probe again.
    assert facts_of(node.properties)["attempted_operations"] == ["recon.service_discovery"]
