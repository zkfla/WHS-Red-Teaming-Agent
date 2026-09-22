"""Capability Layer contracts: tool metadata, invocation, and result.

Every MCP tool result — regardless of which MCP server produced it — is
normalized into ``ToolResult`` before anything else in the framework
touches it. External MCP SDK objects never cross this boundary.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator

from redteam.core.enums import RiskLevel, ToolHealthStatus
from redteam.core.ids import ActionId, AgentId, EngagementId, InvocationId, RunId, ToolId


class ToolDescriptor(BaseModel):
    tool_id: ToolId
    server_id: str
    tool_name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    # Metadata discovered from the MCP server plus operator-approved bindings.
    # ``input_schema`` remains the source of truth for required/optional
    # arguments; the convenience properties below only present that schema in
    # a form that Agent/Registry code can consume safely.
    output_schema: dict[str, Any] = Field(default_factory=dict)
    operations: list[str] = Field(default_factory=list)
    target_types: list[str] = Field(default_factory=list)
    capability_tags: list[str] = Field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.LOW
    health_status: ToolHealthStatus = ToolHealthStatus.UNKNOWN

    @property
    def required_arguments(self) -> tuple[str, ...]:
        required = self.input_schema.get("required", [])
        if not isinstance(required, list):
            return ()
        return tuple(name for name in required if isinstance(name, str))

    @property
    def optional_arguments(self) -> tuple[str, ...]:
        properties = self.input_schema.get("properties", {})
        if not isinstance(properties, dict):
            return ()
        required = set(self.required_arguments)
        return tuple(
            name for name in properties if isinstance(name, str) and name not in required
        )


class ExecutionContext(BaseModel):
    """Minimal run/engagement context a Tool Gateway needs to execute safely."""

    engagement_id: EngagementId
    run_id: RunId
    agent_id: AgentId | None = None
    correlation_id: str | None = None
    dry_run: bool = False


class ToolInvocation(BaseModel):
    invocation_id: InvocationId
    action_id: ActionId
    run_id: RunId
    tool_id: ToolId
    # Canonical, tool-independent intent copied from ActionProposal.operation.
    # The actual MCP remote tool name is resolved from tool_id via ToolDescriptor.
    operation: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: float = 30.0
    execution_context: ExecutionContext
    created_at: datetime

    @model_validator(mode="after")
    def _timeout_must_be_positive(self) -> ToolInvocation:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")
        return self


class ToolError(BaseModel):
    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    invocation_id: InvocationId
    action_id: ActionId
    tool_id: ToolId

    success: bool

    output: dict[str, Any] | str | None = None

    error: ToolError | None = None

    started_at: datetime
    finished_at: datetime

    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _finished_not_before_started(self) -> ToolResult:
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must be >= started_at")
        return self
