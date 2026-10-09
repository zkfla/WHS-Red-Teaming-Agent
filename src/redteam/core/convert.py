"""Mechanical conversions between Core Contracts.

No policy/business logic lives here — only the "given A, produce B"
transforms every team would otherwise duplicate:

    ActionProposal + PolicyDecision -> ToolInvocation
    ToolResult                      -> EvidenceRecord

``build_tool_invocation`` also enforces that a non-ALLOW
``PolicyDecision`` can never become a ``ToolInvocation``. That's a
mechanical guard (reject on a field value), not a policy decision, so it
belongs here rather than in ``redteam.control``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.contracts.policy import PolicyDecision
from redteam.core.contracts.tool import ExecutionContext, ToolInvocation, ToolResult
from redteam.core.enums import EvidenceSourceType, PolicyVerdict
from redteam.core.ids import (
    EngagementId,
    EvidenceId,
    HypothesisId,
    InvocationId,
    RunId,
    ToolId,
    new_id,
)
from redteam.core.tool_binding import SELECTED_TOOL, execution_fingerprint, is_direct


class NonAllowDecisionError(ValueError):
    """Raised when trying to build a ToolInvocation from a non-ALLOW decision."""


def build_tool_invocation(
    proposal: ActionProposal,
    decision: PolicyDecision,
    *,
    tool_id: ToolId,
    timeout_seconds: float = 30.0,
    correlation_id: str | None = None,
    dry_run: bool = False,
) -> ToolInvocation:
    if decision.action_id != proposal.action_id:
        raise ValueError("decision.action_id does not match proposal.action_id")
    if decision.verdict != PolicyVerdict.ALLOW:
        raise NonAllowDecisionError(
            f"cannot build a ToolInvocation from a {decision.verdict.value} PolicyDecision"
        )
    if is_direct(proposal.binding_mode, proposal.tool_id):
        if proposal.tool_id is None or str(tool_id) != str(proposal.tool_id):
            raise ValueError("direct invocation tool_id must equal proposal.tool_id")
        fingerprint = execution_fingerprint(proposal.tool_id, proposal.target, proposal.arguments)
        if decision.execution_fingerprint is not None:
            if decision.execution_fingerprint != fingerprint:
                raise ValueError("decision fingerprint does not match the approved proposal")
        return ToolInvocation(
            invocation_id=InvocationId(new_id("inv")),
            action_id=proposal.action_id,
            run_id=proposal.run_id,
            tool_id=tool_id,
            binding_mode=SELECTED_TOOL,
            operation="",
            arguments=proposal.arguments,
            timeout_seconds=timeout_seconds,
            execution_context=ExecutionContext(
                engagement_id=proposal.engagement_id,
                run_id=proposal.run_id,
                agent_id=proposal.agent_id,
                instance_id=proposal.instance_id,
                approved_target=proposal.target,
                correlation_id=correlation_id,
                dry_run=dry_run,
            ),
            created_at=datetime.now(timezone.utc),
        )
    fingerprint = execution_fingerprint(tool_id, proposal.target, proposal.arguments)
    if decision.execution_fingerprint is not None:
        if decision.execution_fingerprint != fingerprint:
            raise ValueError("decision fingerprint does not match the approved proposal")
    return ToolInvocation(
        invocation_id=InvocationId(new_id("inv")),
        action_id=proposal.action_id,
        run_id=proposal.run_id,
        tool_id=tool_id,
        binding_mode=proposal.binding_mode,
        operation=proposal.operation,
        arguments=proposal.arguments,
        timeout_seconds=timeout_seconds,
        execution_context=ExecutionContext(
            engagement_id=proposal.engagement_id,
            run_id=proposal.run_id,
            agent_id=proposal.agent_id,
            instance_id=proposal.instance_id,
            approved_target=proposal.target,
            correlation_id=correlation_id,
            dry_run=dry_run,
        ),
        created_at=datetime.now(timezone.utc),
    )


def digest_payload(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def execution_input_digest(invocation: ToolInvocation) -> str:
    """Digest the bound execution input and approved subject, when present.

    Local target-free calls keep their historical arguments digest. The
    trusted identity is checked separately on the execution reference.
    """
    target = invocation.execution_context.approved_target
    if target is None:
        return digest_payload(invocation.arguments)
    return digest_payload({
        "arguments": invocation.arguments, "approved_target": target.model_dump(mode="json"),
    })


def tool_result_to_evidence(
    result: ToolResult,
    *,
    run_id: RunId,
    engagement_id: EngagementId,
    hypothesis_id: HypothesisId | None = None,
    summary: str | None = None,
) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=EvidenceId(new_id("ev")),
        run_id=run_id,
        engagement_id=engagement_id,
        source_type=EvidenceSourceType.TOOL,
        source_id=str(result.tool_id),
        action_id=result.action_id,
        invocation_id=result.invocation_id,
        hypothesis_id=hypothesis_id,
        content_type="application/json",
        artifact_ref=None,
        digest=digest_payload(result.model_dump(mode="json")),
        summary=summary,
        created_at=datetime.now(timezone.utc),
        metadata={"success": result.success},
    )
