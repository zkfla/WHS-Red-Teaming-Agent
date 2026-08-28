"""Policy-revalidating MCP server factories for bounded browser and HTTP workers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from whs_redteam.audit import AuditLogger
from whs_redteam.config import EngagementConfig
from whs_redteam.exceptions import CapabilityPolicyViolation
from whs_redteam.models import ActionProposal, Capability, RiskLevel
from whs_redteam.policy.capabilities import CapabilityPolicy
from whs_redteam.policy.capability_catalog import CAPABILITY_CATALOG
from whs_redteam.policy.scope import ScopeGuard


class MCPActionEnvelope(BaseModel):
    """Server-validated subset of ToolCall; credentials are never accepted here."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    call_id: str = Field(min_length=1, max_length=128)
    action_id: str = Field(min_length=1, max_length=128)
    queue_id: str | None = Field(default=None, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    engagement_name: str = Field(min_length=1, max_length=100)
    capability: Capability
    target_url: str
    method: str
    risk_level: RiskLevel
    actor_id: str | None = None
    fixture_id: str | None = Field(default=None, max_length=64)
    approval_verified: bool = False
    authorization_receipt: str | None = Field(default=None, max_length=8192)

    @field_validator("method")
    @classmethod
    def normalize_method(cls, method: str) -> str:
        return method.upper()


class ReceiptVerifier(Protocol):
    def __call__(self, request: MCPActionEnvelope, receipt: str) -> bool: ...


class MCPServerGuard:
    """Re-run engagement policy inside the MCP server trust boundary."""

    def __init__(
        self,
        config: EngagementConfig,
        audit_logger: AuditLogger,
        receipt_verifier: ReceiptVerifier | None = None,
    ) -> None:
        self.config = config
        self.audit_logger = audit_logger
        self.policy = CapabilityPolicy(config, ScopeGuard(config, audit_logger))
        self.receipt_verifier = receipt_verifier

    def authorize(
        self, request: MCPActionEnvelope, expected: Capability
    ) -> ActionProposal:
        if request.engagement_name != self.config.name:
            raise CapabilityPolicyViolation("MCP engagement binding mismatch")
        if request.capability is not expected:
            raise CapabilityPolicyViolation("MCP capability binding mismatch")
        spec = CAPABILITY_CATALOG[expected]
        approval_verified = False
        if spec.approval_required:
            if request.authorization_receipt is None or self.receipt_verifier is None:
                raise CapabilityPolicyViolation(
                    "MCP server requires an independently verified receipt"
                )
            approval_verified = bool(
                self.receipt_verifier(request, request.authorization_receipt)
            )
            if not approval_verified:
                raise CapabilityPolicyViolation("MCP authorization receipt is invalid")
        proposal = ActionProposal(
            capability=request.capability,
            target_url=request.target_url,
            method=request.method,
            risk_level=request.risk_level,
            reason="MCP server-side policy revalidation.",
            actor_id=request.actor_id,
            fixture_id=request.fixture_id,
        )
        approved = self.policy.approve(
            proposal, approval_verified=approval_verified
        )
        self.audit_logger.emit(
            "mcp_server_action_authorized",
            run_id=request.run_id,
            action_id=request.action_id,
            capability=expected.value,
            target_url=approved.target_url,
        )
        return approved


MCPHandler = Callable[
    [MCPActionEnvelope, ActionProposal],
    Awaitable[Mapping[str, Any]],
]


def _mcp_server_class():
    try:
        from mcp.server import MCPServer
    except ImportError as exc:  # pragma: no cover - base install without MCP extra
        raise RuntimeError("install the 'mcp' project extra to create MCP servers") from exc
    return MCPServer


def create_browser_mcp_server(
    guard: MCPServerGuard,
    handler: MCPHandler,
):
    """Create a Browser MCP server exposing only the discovery tool."""

    server = _mcp_server_class()("whs-browser")

    @server.tool(name="discover")
    async def discover(request: MCPActionEnvelope) -> dict[str, Any]:
        approved = guard.authorize(request, Capability.DISCOVERY)
        return dict(await handler(request, approved))

    return server


def create_safe_http_mcp_server(
    guard: MCPServerGuard,
    handler: MCPHandler,
):
    """Create a Safe HTTP MCP server exposing only finding validation."""

    server = _mcp_server_class()("whs-safe-http")

    @server.tool(name="validate_findings")
    async def validate_findings(request: MCPActionEnvelope) -> dict[str, Any]:
        approved = guard.authorize(request, Capability.FINDING_VALIDATION)
        return dict(await handler(request, approved))

    return server


def create_api_schema_mcp_server(
    guard: MCPServerGuard,
    handler: MCPHandler,
):
    """Create a value-free API schema inventory MCP server."""

    server = _mcp_server_class()("whs-api-schema")

    @server.tool(name="summarize_api_schema")
    async def summarize_api_schema(
        request: MCPActionEnvelope,
    ) -> dict[str, Any]:
        approved = guard.authorize(request, Capability.API_SCHEMA)
        return dict(await handler(request, approved))

    return server


def create_actor_session_mcp_server(
    guard: MCPServerGuard,
    handler: MCPHandler,
):
    """Create a metadata-only actor readiness MCP server."""

    server = _mcp_server_class()("whs-actor-session")

    @server.tool(name="summarize_actor_sessions")
    async def summarize_actor_sessions(
        request: MCPActionEnvelope,
    ) -> dict[str, Any]:
        approved = guard.authorize(request, Capability.AUTH_SESSION)
        return dict(await handler(request, approved))

    return server


def create_resource_model_mcp_server(
    guard: MCPServerGuard,
    handler: MCPHandler,
):
    """Create a value-free API resource modeling MCP server."""

    server = _mcp_server_class()("whs-resource-model")

    @server.tool(name="model_resources")
    async def model_resource_inventory(
        request: MCPActionEnvelope,
    ) -> dict[str, Any]:
        approved = guard.authorize(request, Capability.RESOURCE_MODEL)
        return dict(await handler(request, approved))

    return server
