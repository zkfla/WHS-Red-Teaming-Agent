"""End-to-End Test: Agent -> Control Plane -> Capability -> Evidence -> Event -> Attack Graph.

Exercises the Minimal Vertical Slice from instruction.md section 16
using every real skeleton component except the LLM (FakeLLMClient) and
the MCP server (ExampleMCPAdapter, simulated in-process).
"""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.app.bootstrap import build_runtime_deps, run_engagement
from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
)
from redteam.core.contracts.events import EventEnvelope, EventType
from redteam.core.enums import AgentPhase, Verdict
from redteam.core.ids import EngagementId, GraphNodeId


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

    # 13. Run ends in a terminal phase with a recorded verdict.
    assert final_state.phase == AgentPhase.FINISHED
    assert final_state.last_verdict == Verdict.PASS

    # 3-11. The full chain of events fired, in order, all tagged with
    # the right run/engagement.
    expected_in_order = [
        EventType.RUN_STARTED,
        EventType.AGENT_STARTED,
        EventType.ACTION_PROPOSED,
        EventType.POLICY_EVALUATED,
        EventType.ACTION_APPROVED,
        EventType.TOOL_EXECUTION_STARTED,
        EventType.TOOL_EXECUTION_FINISHED,
        EventType.EVIDENCE_CREATED,
        EventType.GRAPH_UPDATED,
        EventType.RUN_FINISHED,
    ]
    assert [e.event_type for e in observed_events] == expected_in_order
    assert all(e.run_id == final_state.run_id for e in observed_events)
    assert all(e.engagement_id == engagement.engagement_id for e in observed_events)

    # 9. Evidence was recorded for the run, correlated to the action.
    evidence = await deps.evidence_store.list_for_run(final_state.run_id)
    assert len(evidence) == 1
    assert evidence[0].action_id is not None
    assert evidence[0].invocation_id is not None
    assert evidence[0].engagement_id == engagement.engagement_id

    # 11. Attack Graph was updated with the target.
    node = await deps.graph_store.get_node(GraphNodeId("example.local"))
    assert node is not None
    assert node.type.value == "host"
