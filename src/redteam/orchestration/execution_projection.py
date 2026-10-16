"""Safe execution selection/outcome projections for planning and read-only UI.

The immutable execution receipt remains authoritative.  These projections keep
only provider declarations, bounded identifiers and code-owned recovery meaning;
they never copy stdout, HTTP bodies, targets, arguments or free-form errors.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from redteam.core.contracts.execution import (
    ExecutionCauseScope,
    ExecutionDiagnosticCategory,
    ExecutionOutcomeProjection,
    ExecutionResultKind,
    ExecutionSelectionProjection,
)
from redteam.core.contracts.policy import PolicyDecision
from redteam.core.contracts.tool import ToolDescriptor, ToolInvocation, ToolResult
from redteam.core.enums import PolicyVerdict
from redteam.core.ports.program_discovery import ProgramExecutionEnvironment


def _program(arguments: Mapping[str, Any]) -> tuple[str | None, str | None]:
    raw = arguments.get("program")
    if not isinstance(raw, Mapping):
        return None, None
    name = raw.get("name")
    executable = raw.get("executable")
    return (
        name if isinstance(name, str) and 0 < len(name) <= 256 else None,
        executable if isinstance(executable, str) and 0 < len(executable) <= 4096 else None,
    )


def _receipt_provider(metadata: Mapping[str, Any]) -> str | None:
    value = metadata.get("receipt_provider")
    return value if isinstance(value, str) and 0 < len(value) <= 128 else None


def execution_selection(
    descriptor: ToolDescriptor | None,
    invocation: ToolInvocation,
    environment: ProgramExecutionEnvironment | dict[str, Any] | None,
) -> ExecutionSelectionProjection:
    """Capture one descriptor/inventory-derived route without opening it."""

    program, executable = _program(invocation.arguments)
    parsed = None
    if environment is not None:
        try:
            parsed = ProgramExecutionEnvironment.model_validate(environment)
        except (TypeError, ValueError):
            parsed = None
    identity = str(invocation.tool_id)
    routes = list(parsed.routes) if parsed is not None else []
    route = next((item for item in routes if identity in item.tool_ids), None)
    declared_route = descriptor.execution_route if descriptor is not None else None
    if route is None and declared_route is not None:
        # A protocol adapter may share the prepared target substrate without
        # being a program-execution tool itself.  The provider-declared target
        # route is still the relevant readiness declaration.
        route = next((item for item in routes if item.purpose == declared_route), None)
    if route is not None:
        return ExecutionSelectionProjection(
            route=route.purpose,
            route_status=route.status,
            route_reason_code=route.reason_code,
            selected_program=program,
            program_executable=executable,
        )
    return ExecutionSelectionProjection(
        route=declared_route,
        route_status="unknown",
        route_reason_code="route_metadata_unavailable",
        selected_program=program,
        program_executable=executable,
    )


def waiting_outcome(reason: str) -> ExecutionOutcomeProjection | None:
    if reason == "approval":
        return ExecutionOutcomeProjection(
            result_kind="not_executed",
            diagnostic_category="approval_pending",
            diagnostic_code="approval_pending",
            cause_scope="policy_wait",
            resume_condition=(
                "Resume only after the original approval ID is approved for the unchanged "
                "execution fingerprint. Do not create a replacement invocation."
            ),
        )
    if reason == "resource":
        return ExecutionOutcomeProjection(
            result_kind="not_executed",
            diagnostic_category="resource_wait",
            diagnostic_code="resource_wait",
            cause_scope="policy_wait",
            resume_condition=(
                "Resume when the original resource reservation can be admitted; preserve the "
                "pending invocation identity."
            ),
        )
    return None


def denied_outcome(decision: PolicyDecision) -> ExecutionOutcomeProjection:
    scoped = "scope" in decision.matched_rules
    return ExecutionOutcomeProjection(
        result_kind="not_executed",
        success=False,
        diagnostic_category="scope_denied" if scoped else "provider_failure",
        diagnostic_code="scope_denied" if scoped else "policy_admission_denied",
        cause_scope="target_prerequisite" if scoped else "policy_wait",
        resume_condition=(
            "Resume only after the operator changes the registered scope for this exact target; "
            "discovery does not expand scope."
            if scoped
            else "Resume only after the controlling policy condition changes; do not clone the "
                 "Task to bypass the denial."
        ),
    )


def _failure_projection(
    result: ToolResult, *, route: str | None,
) -> ExecutionOutcomeProjection:
    code = result.error.code if result.error is not None else "execution_failed"
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    category: ExecutionDiagnosticCategory
    scope: ExecutionCauseScope
    resume: str
    if metadata.get("direct_target_network") == "detach_unverified":
        category, scope, resume = (
            "detach_unknown", "runtime_contract",
            "Reconcile the original attachment/detachment state. The physical call is UNKNOWN "
            "and must not be submitted again.",
        )
    elif code == "shell_program_privilege_unsupported":
        category, scope, resume = (
            "unsupported_privilege", "target_prerequisite",
            "Select a declared unprivileged mode or start a new Run with an operator-selected "
            "narrow privileged profile. The shared workroom is not elevated.",
        )
    elif code in {"shell_program_not_discovered", "shell_program_metadata_invalid"}:
        category, scope, resume = (
            "program_absent", "target_prerequisite",
            "Resume after the bound image/instance inventory contains the program. An image or "
            "manifest change requires a new Run.",
        )
    elif code.endswith("route_unavailable") or code.endswith("route_unsupported"):
        category, scope, resume = (
            "route_unavailable", "target_prerequisite",
            "Resume when the bound execution route becomes available, or create different work "
            "only if a declared alternative route can satisfy the same purpose.",
        )
    elif "scope_denied" in code or code.endswith("pin_denied"):
        category, scope, resume = (
            "scope_denied", "target_prerequisite",
            "Resume only after an operator-authorized scope change for the exact destination; "
            "do not reinterpret the denial as a negative security result.",
        )
    elif "timeout" in code and route == "target":
        category, scope, resume = (
            "target_timeout", "target_prerequisite",
            "Resume after target reachability or the registered timeout prerequisite changes. "
            "The timeout is not a candidate refutation.",
        )
    elif code.startswith("target_http_") and code not in {
        "target_http_redirect_scope_denied",
    }:
        category, scope, resume = (
            "response_unavailable", "target_prerequisite",
            "Resume when the registered target route can produce a canonical response receipt; "
            "absence of a response is not evidence of absence.",
        )
    else:
        category, scope, resume = (
            "provider_failure", "unknown",
            "Preserve the original failure and reconsider only after its provider prerequisite "
            "or a declared alternative changes.",
        )
    executed_program = metadata.get("program_name")
    program_executable = metadata.get("program_executable")
    return ExecutionOutcomeProjection(
        result_kind="provider_failure",
        success=False,
        receipt_provider=_receipt_provider(metadata),
        executed_program=(
            executed_program
            if isinstance(executed_program, str) and 0 < len(executed_program) <= 256
            else None
        ),
        program_executable=(
            program_executable
            if isinstance(program_executable, str) and 0 < len(program_executable) <= 4096
            else None
        ),
        diagnostic_category=category,
        diagnostic_code=code,
        cause_scope=scope,
        resume_condition=resume,
    )


def execution_outcome(
    result: ToolResult,
    *,
    selection: ExecutionSelectionProjection | None,
    mapped_evidence_metadata: Mapping[str, Any] | None = None,
) -> ExecutionOutcomeProjection:
    """Project a settled original; only mapper output can claim canonical receipt."""

    if not result.success:
        return _failure_projection(result, route=selection.route if selection else None)
    executed_program = result.metadata.get("program_name")
    program_executable = result.metadata.get("program_executable")
    if not isinstance(executed_program, str) or not 0 < len(executed_program) <= 256:
        executed_program = None
    if not isinstance(program_executable, str) or not 0 < len(program_executable) <= 4096:
        program_executable = None
    mapped = mapped_evidence_metadata or {}
    if mapped.get("mapping_status") == "mapped" and (
        isinstance(mapped.get("http_observation"), Mapping)
        or isinstance(mapped.get("session_comparison"), Mapping)
    ):
        kind: ExecutionResultKind = "canonical_protocol_receipt"
    elif result.metadata.get("execution_kind") == "program" or (
        isinstance(result.output, Mapping) and "stdout" in result.output
    ):
        kind = "generic_program_output"
    else:
        kind = "tool_result"
    return ExecutionOutcomeProjection(
        result_kind=kind,
        success=True,
        receipt_provider=_receipt_provider(result.metadata),
        executed_program=executed_program,
        program_executable=program_executable,
    )


def unknown_outcome(result: ToolResult | None = None) -> ExecutionOutcomeProjection:
    metadata = result.metadata if result is not None and isinstance(result.metadata, dict) else {}
    detach = metadata.get("direct_target_network") == "detach_unverified"
    return ExecutionOutcomeProjection(
        result_kind="unknown",
        success=None,
        receipt_provider=_receipt_provider(metadata),
        diagnostic_category="detach_unknown" if detach else "execution_unknown",
        diagnostic_code=(
            "direct_target_disconnect_failed" if detach else "execution_outcome_unknown"
        ),
        cause_scope="runtime_contract",
        resume_condition=(
            "Reconcile the original attachment and physical invocation; never replay it."
            if detach
            else "Recover the original invocation from its receipt/provider handle. Do not "
                 "submit the physical call again."
        ),
    )


def policy_outcome(decision: PolicyDecision) -> ExecutionOutcomeProjection | None:
    if decision.verdict is PolicyVerdict.DENY:
        return denied_outcome(decision)
    if decision.verdict is PolicyVerdict.REQUIRE_APPROVAL:
        return waiting_outcome("approval")
    return None
