"""Native tool progress depends on changed task inputs or original outcomes."""

from datetime import datetime, timedelta, timezone

from redteam.core.contracts.target import HostTarget
from redteam.core.contracts.tool import ExecutionContext, ToolInvocation, ToolResult
from redteam.core.ids import (
    ActionId,
    AgentId,
    AgentInstanceId,
    EngagementId,
    InvocationId,
    RunId,
    ToolId,
)
from redteam.orchestration.nodes.react import (
    _same_dispatch_input,
    _same_original_outcome,
)

NOW = datetime(2026, 10, 16, tzinfo=timezone.utc)


def _invocation(identity: str, *, action: str | None = None, arguments=None,
                tool: str = "inventory.enumerate", correlation: str | None = None,
                target=None):
    action_id = action or f"action-{identity}"
    return ToolInvocation(
        invocation_id=InvocationId(f"inv-{identity}"),
        action_id=ActionId(action_id),
        run_id=RunId("run-1"),
        tool_id=ToolId(tool),
        binding_mode="selected_tool",
        arguments=arguments or {},
        execution_context=ExecutionContext(
            engagement_id=EngagementId("engagement-1"),
            run_id=RunId("run-1"),
            agent_id=AgentId("role-1"),
            instance_id=AgentInstanceId("instance-1"),
            approved_target=target,
            correlation_id=correlation,
        ),
        timeout_seconds=30,
        created_at=NOW,
    )


def _result(invocation: ToolInvocation, output: dict, *, started_at=NOW,
            metadata=None) -> ToolResult:
    return ToolResult(
        invocation_id=invocation.invocation_id,
        action_id=invocation.action_id,
        tool_id=invocation.tool_id,
        success=True,
        output=output,
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=1),
        metadata=metadata or {},
    )


def test_same_task_input_and_same_original_outcome_are_unchanged_progress():
    previous = _invocation("old", correlation="corr-old")
    current = _invocation("new", correlation="corr-new")

    assert _same_dispatch_input(
        current, previous,
        current_purpose_hash="purpose-1", previous_purpose_hash="purpose-1",
    )
    assert _same_original_outcome(
        _result(current, {"items": ["a", "b"]}, started_at=NOW + timedelta(minutes=1)),
        _result(previous, {"items": ["a", "b"]}),
    )


def test_changed_inventory_result_is_new_observation():
    previous = _invocation("old")
    current = _invocation("new")

    assert not _same_original_outcome(
        _result(current, {"items": ["a", "b", "c"]}),
        _result(previous, {"items": ["a", "b"]}),
    )


def test_same_http_status_with_different_actual_receipt_is_not_unchanged():
    previous = _invocation("old", tool="web.request")
    current = _invocation("new", tool="web.request")

    anonymous = _result(previous, {"status_code": 404, "actual_session_ref": None})
    named = _result(current, {"status_code": 404, "actual_session_ref": "registered-ref"})
    assert not _same_original_outcome(named, anonymous)


def test_changed_tool_arguments_or_task_purpose_are_not_collapsed():
    previous = _invocation("old", arguments={"scope": "one"})
    changed_arguments = _invocation("new", arguments={"scope": "two"})
    changed_tool = _invocation("other", tool="inventory.other", arguments={"scope": "one"})
    changed_target = _invocation(
        "target-change", arguments={"scope": "one"}, target=HostTarget(host="other.local"),
    )

    assert not _same_dispatch_input(
        changed_arguments, previous,
        current_purpose_hash="purpose-1", previous_purpose_hash="purpose-1",
    )
    assert not _same_dispatch_input(
        changed_tool, previous,
        current_purpose_hash="purpose-1", previous_purpose_hash="purpose-1",
    )
    assert not _same_dispatch_input(
        changed_target, previous,
        current_purpose_hash="purpose-1", previous_purpose_hash="purpose-1",
    )
    assert not _same_dispatch_input(
        previous, previous,
        current_purpose_hash="purpose-2", previous_purpose_hash="purpose-1",
    )
