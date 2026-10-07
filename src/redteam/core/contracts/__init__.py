"""Pydantic Core Contracts — the Source of Truth for domain data.

No framework library (LangGraph, LiteLLM, MCP client, docker SDK, ...)
may be imported here. Everything else in the repo imports these models
instead of redefining ad hoc dicts/dataclasses.
"""

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
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.contracts.graph import Finding, GraphEdge, GraphNode, Hypothesis
from redteam.core.contracts.mission import MissionPlan, MissionTask, OperatorIntent, Provenance
from redteam.core.contracts.policy import Approval, ControlContext, PolicyDecision
from redteam.core.contracts.target import (
    AnyTarget,
    AssetTarget,
    DeviceTarget,
    DomainTarget,
    EndpointTarget,
    GraphNodeTarget,
    HostTarget,
    PrincipalTarget,
    ServiceTarget,
    TargetRef,
    UrlTarget,
    target_display_name,
    target_graph_node_id,
)
from redteam.core.contracts.tool import (
    PROVIDER_METADATA_AVAILABILITY_IMPACT,
    PROVIDER_METADATA_COMMAND_FIELDS,
    PROVIDER_METADATA_REQUIRES_TARGET,
    PROVIDER_METADATA_TARGET_BINDING,
    ExecutionContext,
    ToolDescriptor,
    ToolError,
    ToolInvocation,
    ToolResult,
    ToolTargetBinding,
)
from redteam.core.tool_binding import (
    LEGACY_ACTION,
    SELECTED_TOOL,
    BindingMode,
    execution_display_key,
    execution_fingerprint,
    is_direct,
    normalize_arguments,
    normalize_json,
)

__all__ = [
    "ActionProposal",
    "TargetRef",
    "AnyTarget",
    "HostTarget",
    "DomainTarget",
    "UrlTarget",
    "EndpointTarget",
    "ServiceTarget",
    "PrincipalTarget",
    "AssetTarget",
    "DeviceTarget",
    "GraphNodeTarget",
    "target_graph_node_id",
    "target_display_name",
    "Engagement",
    "EngagementScope",
    "RuleOfEngagement",
    "ExecutionBudget",
    "RateLimitPolicy",
    "ApprovalPolicy",
    "EvidenceRecord",
    "EventEnvelope",
    "EventType",
    "GraphNode",
    "GraphEdge",
    "Hypothesis",
    "Finding",
    "MissionPlan",
    "MissionTask",
    "OperatorIntent",
    "Provenance",
    "ControlContext",
    "PolicyDecision",
    "Approval",
    "ToolDescriptor",
    "ExecutionContext",
    "ToolInvocation",
    "ToolError",
    "ToolResult",
    "ToolTargetBinding",
    "PROVIDER_METADATA_COMMAND_FIELDS",
    "PROVIDER_METADATA_REQUIRES_TARGET",
    "PROVIDER_METADATA_TARGET_BINDING",
    "PROVIDER_METADATA_AVAILABILITY_IMPACT",
    "BindingMode",
    "LEGACY_ACTION",
    "SELECTED_TOOL",
    "execution_display_key",
    "execution_fingerprint",
    "is_direct",
    "normalize_arguments",
    "normalize_json",
]
