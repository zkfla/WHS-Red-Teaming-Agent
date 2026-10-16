"""Plan 2d execution route/result diagnostics are stored without original bodies."""

from datetime import datetime, timezone

import pytest

from redteam.core.contracts.execution import ExecutionSelectionProjection
from redteam.core.contracts.policy import PolicyDecision
from redteam.core.contracts.tool import (
    ExecutionContext,
    ToolDescriptor,
    ToolError,
    ToolInvocation,
    ToolResult,
)
from redteam.core.enums import PolicyVerdict
from redteam.orchestration.execution_projection import (
    execution_outcome,
    execution_selection,
    policy_outcome,
    unknown_outcome,
)

NOW = datetime(2026, 10, 16, tzinfo=timezone.utc)


def invocation(arguments=None, *, tool_id="analysis.shell_target_run"):
    return ToolInvocation(
        invocation_id="inv-1",
        action_id="action-1",
        run_id="run-1",
        tool_id=tool_id,
        arguments=arguments or {},
        execution_context=ExecutionContext(
            engagement_id="eng-1", run_id="run-1", instance_id="instance-1",
        ),
        created_at=NOW,
    )


def result(*, code=None, metadata=None, output=None, success=None):
    ok = code is None if success is None else success
    return ToolResult(
        invocation_id="inv-1",
        action_id="action-1",
        tool_id="analysis.shell_target_run",
        success=ok,
        output=output,
        error=None if ok else ToolError(code=code or "failed", message="bounded"),
        started_at=NOW,
        finished_at=NOW,
        metadata=metadata or {},
    )


def target_selection():
    return ExecutionSelectionProjection(
        route="target",
        route_status="target_ready",
        route_reason_code="target_route_configured",
    )


def test_selection_uses_inventory_route_and_keeps_structured_program_bounded():
    selected = execution_selection(
        ToolDescriptor(
            tool_id="analysis.shell_target_run",
            server_id="shell-workroom",
            tool_name="shell_target_run",
            requires_target=True,
        ),
        invocation({
            "program": {"name": "curl", "executable": "/usr/bin/curl", "argv": []},
        }),
        {
            "version": 1,
            "source_id": "shell-workroom",
            "routes": [{
                "purpose": "target",
                "status": "target_ready",
                "reason_code": "target_route_configured",
                "tool_ids": ["analysis.shell_target_run"],
            }],
            "unsupported_modes": ["raw_socket"],
            "target_execution_verified": False,
            "execution_permission_granted": False,
        },
    )
    assert selected.model_dump() == {
        "route": "target",
        "route_status": "target_ready",
        "route_reason_code": "target_route_configured",
        "selected_program": "curl",
        "program_executable": "/usr/bin/curl",
    }


def test_protocol_adapter_uses_only_its_provider_declared_route():
    environment = {
        "version": 1,
        "source_id": "shell-workroom",
        "routes": [{
            "purpose": "target",
            "status": "target_ready",
            "reason_code": "target_route_configured",
            "tool_ids": ["analysis.shell_target_run"],
        }],
        "unsupported_modes": [],
        "target_execution_verified": False,
        "execution_permission_granted": False,
    }
    protocol = execution_selection(
        ToolDescriptor(
            tool_id="web.kali_http.request",
            server_id="kali-http",
            tool_name="request",
            requires_target=True,
            execution_route="target",
        ),
        invocation(tool_id="web.kali_http.request"),
        environment,
    )
    unrelated = execution_selection(
        ToolDescriptor(
            tool_id="browser.request",
            server_id="browser",
            tool_name="request",
            requires_target=True,
        ),
        invocation(tool_id="browser.request"),
        environment,
    )
    assert protocol.route == "target"
    assert protocol.route_status == "target_ready"
    assert unrelated.route is None
    assert unrelated.route_status == "unknown"


@pytest.mark.parametrize(
    ("code", "metadata", "category", "scope"),
    [
        ("gated_target_route_unavailable", {}, "route_unavailable", "target_prerequisite"),
        ("shell_program_privilege_unsupported", {}, "unsupported_privilege",
         "target_prerequisite"),
        ("direct_target_scope_denied", {}, "scope_denied", "target_prerequisite"),
        ("target_http_response_timeout", {}, "target_timeout", "target_prerequisite"),
        ("shell_program_not_discovered", {}, "program_absent", "target_prerequisite"),
        ("target_http_connection_refused", {}, "response_unavailable",
         "target_prerequisite"),
        ("direct_target_disconnect_failed", {"direct_target_network": "detach_unverified"},
         "detach_unknown", "runtime_contract"),
    ],
)
def test_failures_have_distinct_code_owned_resume_conditions(code, metadata, category, scope):
    projected = execution_outcome(
        result(code=code, metadata=metadata), selection=target_selection(),
    )
    assert projected.diagnostic_category == category
    assert projected.diagnostic_code == code
    assert projected.cause_scope == scope
    assert projected.resume_condition
    assert projected.success is False


def test_generic_stdout_and_mapper_validated_protocol_receipt_are_distinct():
    generic = execution_outcome(
        result(
            metadata={
                "execution_kind": "program",
                "program_name": "curl",
                "program_executable": "/usr/bin/curl",
            },
            output={"stdout": "HTTP/1.1 200 OK"},
        ),
        selection=target_selection(),
    )
    canonical = execution_outcome(
        result(
            metadata={"program_name": "python3", "program_executable": "/usr/bin/python3"},
            output={"http_receipt": {"response": {"status_code": 200}}},
        ),
        selection=target_selection(),
        mapped_evidence_metadata={"mapping_status": "mapped", "http_observation": {}},
    )
    unvalidated = execution_outcome(
        result(output={"http_receipt": {"response": {"status_code": 200}}}),
        selection=target_selection(),
    )
    assert generic.result_kind == "generic_program_output"
    assert generic.executed_program == "curl"
    assert canonical.result_kind == "canonical_protocol_receipt"
    assert canonical.executed_program == "python3"
    assert unvalidated.result_kind == "tool_result"


def test_policy_wait_scope_denial_and_unknown_never_become_success():
    pending = policy_outcome(PolicyDecision(
        action_id="action-1", verdict=PolicyVerdict.REQUIRE_APPROVAL,
        approval_id="approval-1", evaluated_at=NOW,
    ))
    denied = policy_outcome(PolicyDecision(
        action_id="action-1", verdict=PolicyVerdict.DENY,
        matched_rules=["scope"], evaluated_at=NOW,
    ))
    unknown = unknown_outcome()
    assert pending.diagnostic_category == "approval_pending" and pending.success is None
    assert denied.diagnostic_category == "scope_denied" and denied.success is False
    assert unknown.diagnostic_category == "execution_unknown" and unknown.success is None
    assert "Do not submit" in unknown.resume_condition
