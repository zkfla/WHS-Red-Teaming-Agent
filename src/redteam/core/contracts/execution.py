"""Provider-independent execution references and read-only recovery answers.

A reference correlates an approved invocation with provider-owned state.
It never authorizes execution. Only its provider interprets the opaque
handle. Completion describes execution, not an observation or achieved goal.
State describes the referenced invocation's operation. A durable job-start
acknowledgment may be complete while the separately registered job is still
running; its original acknowledgment is not the job's eventual raw result.
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from redteam.core.contracts.tool import ToolError
from redteam.core.ids import (
    ActionId,
    AgentInstanceId,
    EngagementId,
    EvidenceId,
    InvocationId,
    RunId,
    TaskId,
    ToolId,
)


class ExecutionState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class ExecutionReference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    engagement_id: EngagementId
    run_id: RunId
    instance_id: AgentInstanceId | None = None
    action_id: ActionId
    invocation_id: InvocationId
    tool_id: ToolId
    input_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    provider_handle: dict[str, Any]

    @model_validator(mode="after")
    def _bounded_json_handle(self) -> ExecutionReference:
        try:
            serialized = json.dumps(self.provider_handle, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("execution handle must be JSON data") from exc
        if not self.provider_handle or len(serialized) > 16384:
            raise ValueError("execution handle must be nonempty and bounded")
        return self


class ExecutionRecoveryAnswer(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    state: ExecutionState
    verified: bool = False
    uncertain: bool = True
    result_available: bool = False
    # Execution termination is independent of knowing the operation's outcome.
    # Only a verified provider proof (including its execution route) may set it.
    execution_terminated: bool = False
    error: ToolError | None = None

    @model_validator(mode="after")
    def _termination_proof(self):
        if self.execution_terminated and (
            not self.verified or self.state in {ExecutionState.PENDING, ExecutionState.RUNNING}
        ):
            raise ValueError("execution_termination_requires_verified_nonrunning_state")
        return self


class ExecutionSelectionProjection(BaseModel):
    """Bounded execution choice captured before provider entry.

    This is display/planning context, not admission.  It records the route
    declaration and optional structured program selected by the already-bound
    invocation so restart readers do not have to infer either from tool names.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    route: Literal["local", "target", "install"] | None = None
    route_status: Literal[
        "local_only", "target_ready", "install_ready",
        "configured_unverified", "unavailable", "unknown",
    ] = "unknown"
    route_reason_code: str = Field(default="route_metadata_unavailable", max_length=128)
    selected_program: str | None = Field(default=None, min_length=1, max_length=256)
    program_executable: str | None = Field(default=None, min_length=1, max_length=4096)


ExecutionResultKind = Literal[
    "generic_program_output", "canonical_protocol_receipt", "tool_result",
    "provider_failure", "not_executed", "unknown",
]
ExecutionDiagnosticCategory = Literal[
    "route_unavailable", "unsupported_privilege", "scope_denied",
    "approval_pending", "resource_wait", "target_timeout", "detach_unknown",
    "program_absent", "response_unavailable", "execution_unknown",
    "provider_failure",
]
ExecutionCauseScope = Literal[
    "model_input", "target_prerequisite", "policy_wait", "runtime_contract", "unknown",
]


class ExecutionOutcomeProjection(BaseModel):
    """Safe result/diagnostic projection; original bytes remain evidence.

    ``result_kind`` separates generic program stdout from a mapper-validated
    protocol receipt.  Diagnostics provide a code-owned resume condition and
    never turn an execution failure into a security verdict.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    result_kind: ExecutionResultKind
    success: bool | None = None
    executed_program: str | None = Field(default=None, min_length=1, max_length=256)
    program_executable: str | None = Field(default=None, min_length=1, max_length=4096)
    diagnostic_category: ExecutionDiagnosticCategory | None = None
    diagnostic_code: str | None = Field(default=None, min_length=1, max_length=128)
    cause_scope: ExecutionCauseScope = "unknown"
    resume_condition: str = Field(default="", max_length=500)


class TaskExecution(BaseModel):
    """Central execution bookkeeping; original bodies remain immutable evidence.

    ``input_digest`` identifies the exact proposal, never a semantic duplicate.
    A terminal state alone cannot release ``resource_execution_pending``.
    This record supplies no execution permission or automatic retry policy.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    engagement_id: EngagementId
    run_id: RunId
    task_id: TaskId
    instance_id: AgentInstanceId
    ownership_version: int = Field(ge=1)
    # None is retained historical uncertainty, never admission for a new start.
    goal_revision: int | None = Field(default=1, ge=1)
    action_id: ActionId
    invocation_id: InvocationId
    model_tool_call_id: str = Field(min_length=1)
    tool_id: ToolId
    input_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    proposal_evidence_id: EvidenceId
    # Canonical target identity checked against the immutable submission.
    # Older executions without this identity conservatively occupy every target.
    target_node_id: str | None = Field(default=None, min_length=1)
    target_identity_verified: bool = False
    approval_ref: str | None = None
    approval_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    reservation_refs: list[str] = Field(default_factory=list)
    state: ExecutionState = ExecutionState.PENDING
    wait_reason: Literal["approval", "job", "resource"] | None = None
    job_ref: str | None = None
    result_evidence_id: EvidenceId | None = None
    # This proof returns capacity only; UNKNOWN and resource fences remain intact.
    termination_evidence_id: EvidenceId | None = None
    resource_keys: list[str] = Field(default_factory=list)
    resource_execution_pending: bool = False
    resource_lease_until: float = Field(default=0, ge=0, allow_inf_nan=False)
    selection: ExecutionSelectionProjection | None = None
    outcome: ExecutionOutcomeProjection | None = None

    @model_serializer(mode="wrap")
    def serialize_optional_projection(self, handler):
        value = handler(self)
        if self.termination_evidence_id is None:
            value.pop("termination_evidence_id", None)
        if self.selection is None:
            value.pop("selection", None)
        if self.outcome is None:
            value.pop("outcome", None)
        return value

    @model_validator(mode="after")
    def _identity_and_wait(self) -> TaskExecution:
        identities = (self.engagement_id, self.run_id, self.task_id, self.instance_id,
                      self.action_id, self.invocation_id, self.tool_id,
                      self.model_tool_call_id, self.proposal_evidence_id)
        if any(not str(item).strip() for item in identities):
            raise ValueError("execution requires original nonblank identities")
        if any(not item.strip() for item in self.resource_keys):
            raise ValueError("resource key must be nonblank")
        self.resource_keys = sorted({item.strip() for item in self.resource_keys})
        if self.wait_reason == "job" and not self.job_ref:
            raise ValueError("job wait requires original job reference")
        if self.wait_reason == "approval" and (
            not self.approval_ref or not self.approval_fingerprint
        ):
            raise ValueError("approval wait requires original approval and exact fingerprint")
        if self.resource_execution_pending and not self.resource_keys:
            raise ValueError("pending resource execution requires resource keys")
        return self
