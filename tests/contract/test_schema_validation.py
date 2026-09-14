"""Contract Test: Core Contracts validate/round-trip as expected."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import TypeAdapter, ValidationError

from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
    RuleOfEngagement,
)
from redteam.core.contracts.events import EventEnvelope, EventType
from redteam.core.contracts.target import (
    AssetTarget,
    DomainTarget,
    GraphNodeTarget,
    HostTarget,
    PrincipalTarget,
    TargetRef,
)
from redteam.core.contracts.tool import ExecutionContext, ToolInvocation, ToolResult
from redteam.core.ids import (
    ActionId,
    AgentId,
    AssetId,
    EngagementId,
    EventId,
    GraphNodeId,
    InvocationId,
    PrincipalId,
    RunId,
    ToolId,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_action_proposal_round_trip() -> None:
    proposal = ActionProposal(
        action_id=ActionId("action_1"),
        run_id=RunId("run_1"),
        engagement_id=EngagementId("eng_1"),
        agent_id=AgentId("agent_1"),
        capability="recon.example",
        operation="ping",
        target=HostTarget(host="host_1"),
        reason="test",
        created_at=_now(),
    )
    restored = ActionProposal.model_validate_json(proposal.model_dump_json())
    assert restored == proposal


def test_action_proposal_requires_reason() -> None:
    with pytest.raises(ValidationError):
        ActionProposal(
            action_id=ActionId("action_1"),
            run_id=RunId("run_1"),
            engagement_id=EngagementId("eng_1"),
            agent_id=AgentId("agent_1"),
            capability="recon.example",
            operation="ping",
            target=HostTarget(host="host_1"),
            created_at=_now(),
        )  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "target",
    [
        HostTarget(host="a.local"),
        DomainTarget(domain="a.local"),
        PrincipalTarget(principal_id=PrincipalId("p_1")),
        AssetTarget(asset_id=AssetId("asset_1")),
        GraphNodeTarget(node_id=GraphNodeId("node_1")),
    ],
)
def test_target_ref_discriminated_union_round_trips_every_kind(target) -> None:
    adapter: TypeAdapter = TypeAdapter(TargetRef)
    restored = adapter.validate_json(adapter.dump_json(target))
    assert restored == target


def test_target_ref_rejects_unknown_kind() -> None:
    adapter: TypeAdapter = TypeAdapter(TargetRef)
    with pytest.raises(ValidationError):
        adapter.validate_python({"kind": "spaceship", "value": "nope"})


def test_engagement_schema() -> None:
    engagement = Engagement(
        engagement_id=EngagementId("eng_1"),
        name="Test",
        scope=EngagementScope(allowed_hosts=["a.local"]),
        rules_of_engagement=[
            RuleOfEngagement(name="roe-1", allowed_capabilities=["recon.example"])
        ],
        execution_budget=ExecutionBudget(max_actions=10),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=10),
        approval_policy=ApprovalPolicy(),
        created_at=_now(),
    )
    assert engagement.scope.allowed_hosts == ["a.local"]
    assert engagement.rules_of_engagement[0].allowed_capabilities == ["recon.example"]


def test_execution_budget_rejects_negative_values() -> None:
    with pytest.raises(ValidationError):
        ExecutionBudget(max_actions=-1)


def test_event_envelope_uses_canonical_event_types() -> None:
    event = EventEnvelope(
        event_id=EventId("evt_1"),
        event_type=EventType.RUN_STARTED,
        run_id=RunId("run_1"),
        engagement_id=EngagementId("eng_1"),
        source="test",
        timestamp=_now(),
    )
    assert event.event_type == "RunStarted"


def test_tool_invocation_rejects_nonpositive_timeout() -> None:
    with pytest.raises(ValidationError):
        ToolInvocation(
            invocation_id=InvocationId("inv_1"),
            action_id=ActionId("action_1"),
            run_id=RunId("run_1"),
            tool_id=ToolId("tool_1"),
            operation="ping",
            timeout_seconds=0,
            execution_context=ExecutionContext(
                engagement_id=EngagementId("eng_1"), run_id=RunId("run_1")
            ),
            created_at=_now(),
        )


def test_tool_result_normalizes_output_shape() -> None:
    result = ToolResult(
        invocation_id=InvocationId("inv_1"),
        action_id=ActionId("action_1"),
        tool_id=ToolId("tool_1"),
        success=True,
        output={"ok": True},
        started_at=_now(),
        finished_at=_now(),
    )
    assert result.success is True
    assert result.error is None


def test_tool_result_rejects_finished_before_started() -> None:
    started = _now()
    with pytest.raises(ValidationError):
        ToolResult(
            invocation_id=InvocationId("inv_1"),
            action_id=ActionId("action_1"),
            tool_id=ToolId("tool_1"),
            success=True,
            started_at=started,
            finished_at=started - timedelta(seconds=1),
        )
