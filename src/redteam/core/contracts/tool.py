"""Capability Layer contracts: tool metadata, invocation, and result.

Every MCP tool result — regardless of which MCP server produced it — is
normalized into ``ToolResult`` before anything else in the framework
touches it. External MCP SDK objects never cross this boundary.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from redteam.core.contracts.target import TargetRef
from redteam.core.enums import RiskLevel, ToolHealthStatus
from redteam.core.ids import (
    ActionId,
    AgentId,
    AgentInstanceId,
    EngagementId,
    InvocationId,
    RunId,
    ToolId,
)
from redteam.core.tool_binding import LEGACY_ACTION, SELECTED_TOOL, BindingMode


class ToolTargetBinding(BaseModel):
    binds_target_arguments: bool = True
    binds_known_path_from_url: bool = False
    requires_url_target: bool = False
    requires_exact_url_match: bool = False


class ToolResourceBinding(BaseModel):
    """Provider-owned exclusive resource identity, not execution permission.

    A namespace is shared by every provider using the same physical resource.
    Select one input argument or an operator/provider constant. Explicit maps
    canonicalize aliases (e.g. two device refs for one serial). No tool-name
    or argument-suffix inference is permitted.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    namespace: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    argument: str | None = Field(default=None, min_length=1, max_length=128)
    key: str | None = Field(default=None, min_length=1, max_length=4096)
    optional: bool = False
    value_map: dict[str, str] = Field(default_factory=dict, max_length=1024)

    @model_validator(mode="after")
    def _one_identity_source(self) -> ToolResourceBinding:
        if (self.argument is None) == (self.key is None):
            raise ValueError("resource requires exactly one identity source")
        if self.key is not None and (self.optional or self.value_map):
            raise ValueError("constant resource cannot have argument options")
        if any(not k or not v or len(k) > 4096 or len(v) > 4096
               for k, v in self.value_map.items()):
            raise ValueError("resource alias identities must be bounded non-empty strings")
        return self


# Shared provider-metadata keys read from SDK discovery metadata. One
# declaration for every provider; no per-tool central list. Absent keys
# keep the contract defaults.
PROVIDER_METADATA_TARGET_BINDING = "redteam.target_binding"
PROVIDER_METADATA_REQUIRES_TARGET = "redteam.requires_target"
PROVIDER_METADATA_COMMAND_FIELDS = "redteam.command_fields"
PROVIDER_METADATA_AVAILABILITY_IMPACT = "redteam.availability_impact"
PROVIDER_METADATA_RESOURCE_BINDINGS = "redteam.resource_bindings"
PROVIDER_METADATA_RESOURCE_EFFECTS = "redteam.resource_effects"

# Providers attach this when execution may have produced effects but its
# terminal outcome cannot be established. This is independent of success,
# retryability and exception ownership; consumers reconcile the original
# invocation before submitting it again.
EXECUTION_UNCERTAIN_METADATA_KEY = "execution_uncertain"
# An explicit False proves the provider invocation was never entered. True
# reports entry at the execution boundary, not a successful external effect.
EXECUTION_STARTED_METADATA_KEY = "execution_started"


def execution_outcome_uncertain(metadata: Mapping[str, Any]) -> bool:
    """Read uncertainty, using an explicit pre-entry proof for legacy errors.

    A contradictory explicit uncertainty declaration remains held; an
    orchestration exception alone cannot override proved non-entry.
    """

    if metadata.get(EXECUTION_UNCERTAIN_METADATA_KEY) is True:
        return True
    if metadata.get(EXECUTION_STARTED_METADATA_KEY) is False:
        return False
    return bool(metadata.get("orchestration_exception"))


class ToolResourceEffect(BaseModel):
    """Provider-owned resource identity and output paths, not model claims.

    Version 1 is historical decoding only: its local ID has no proven domain.
    Version 2 names the stable backend/tenant domain shared by create/delete
    tools. Providers spanning domains must return a qualified identity value.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    version: Literal[1, 2] = 1
    identity_domain: str | None = Field(default=None, min_length=1, max_length=1024)
    effect: Literal["created", "cleaned"]
    resource_type: str = Field(min_length=1, max_length=128)
    identity_path: list[str | int] = Field(min_length=1, max_length=16)
    confirmation_path: list[str | int] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def validate_identity_domain(self):
        if self.version == 2 and (
            self.identity_domain is None or not self.identity_domain.strip()
        ):
            raise ValueError("resource effect v2 requires a provider identity domain")
        if self.version == 1 and self.identity_domain is not None:
            raise ValueError("resource identity domain requires effect version 2")
        return self

    @model_serializer(mode="wrap")
    def serialize_identity(self, handler):
        value = handler(self)
        if self.identity_domain is None:
            value.pop("identity_domain", None)
        return value


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
    output_schema: dict[str, Any] | None = None
    # None means unspecified; an explicit empty list declares no created resources.
    resource_effects: list[ToolResourceEffect] | None = None
    operations: list[str] = Field(default_factory=list)
    target_types: list[str] = Field(default_factory=list)
    capability_tags: list[str] = Field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.LOW
    health_status: ToolHealthStatus = ToolHealthStatus.UNKNOWN
    # Declared by the tool provider for its actual operation semantics.
    retry_safe: bool = False
    @model_serializer(mode="wrap")
    def serialize_optional_effects(self, handler):
        value = handler(self)
        # Preserve existing tool/binding fingerprints when the feature is absent.
        if self.resource_effects is None:
            value.pop("resource_effects", None)
        return value

    # Input argument -> shared target value (host/url/port/path/asset_ref/etc.).
    # Providers declare different remote argument names without central edits.
    argument_bindings: dict[str, str] = Field(default_factory=dict)
    # Provider-declared input fields transmitted as text to an external service.
    outbound_text_fields: list[str] = Field(default_factory=list)
    # Provider-declared target semantics consumed by direct selected-tool calls.
    target_binding: ToolTargetBinding = Field(default_factory=ToolTargetBinding)
    # Provider-owned declaration: execution uses configured infrastructure,
    # not an engagement target selected by the caller. Argument binding and
    # compatible target kinds alone do not imply this permission.
    requires_target: bool = True
    # Provider-declared ordered input components of ONE command, not
    # independent text fields. Consumers assemble strings and string-array
    # elements in field/array order for command-pattern inspection.
    # Example: ["executable", "args"]. Empty means no command input.
    command_fields: list[str] = Field(default_factory=list)
    # Provider-declared availability effect for its actual operation semantics.
    # Defaults to False ("no known impact") so existing descriptors are
    # unaffected; a provider whose tool genuinely can affect availability
    # must set this explicitly. Never inferred from tool name, risk level,
    # or command content.
    availability_impact: bool = False
    # Empty means no declared shared resources, NOT proof of parallel safety.
    # Scheduling still retains existing target conflicts and Control checks.
    resource_bindings: list[ToolResourceBinding] = Field(default_factory=list, max_length=32)

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
        return tuple(name for name in properties if isinstance(name, str) and name not in required)


class ExecutionContext(BaseModel):
    """Minimal run/engagement context a Tool Gateway needs to execute safely."""

    engagement_id: EngagementId
    run_id: RunId
    agent_id: AgentId | None = None
    instance_id: AgentInstanceId | None = None
    # Mechanically copied from the ALLOW proposal by the application.
    # Provider target windows cannot infer this authority from a script.
    approved_target: TargetRef | None = None
    correlation_id: str | None = None
    dry_run: bool = False


class ToolInvocation(BaseModel):
    invocation_id: InvocationId
    action_id: ActionId
    run_id: RunId
    tool_id: ToolId
    binding_mode: BindingMode = LEGACY_ACTION
    # Legacy canonical intent copied from ActionProposal.operation; empty for direct calls.
    # The actual MCP remote tool name is resolved from tool_id via ToolDescriptor.
    operation: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: float = 30.0
    execution_context: ExecutionContext
    created_at: datetime

    @model_validator(mode="after")
    def _timeout_must_be_positive(self) -> ToolInvocation:
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and > 0")
        if self.binding_mode == SELECTED_TOOL and self.operation:
            raise ValueError("selected_tool invocation must leave operation empty")
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
