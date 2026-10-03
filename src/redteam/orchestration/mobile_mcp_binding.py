"""Run-local MCP eligibility view; discovery health is never rewritten as policy."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from redteam.core.contracts.tool import ToolError, ToolResult
from redteam.core.enums import ToolHealthStatus
from redteam.core.ports.tool_gateway import BeforeToolStart, ToolStartAdmissionGateway
from redteam.orchestration.observations.mapper import register_contribution
from redteam.orchestration.observations.packages import mobsf_observation_package

#: Contribution kind spoken by vendor policies. The composition root
#: owns collection; the name is the draft so both sides can meet.
VENDOR_POLICY_KIND = "vendor_policy"


@dataclass(frozen=True)
class VendorPolicy:
    """One vendor's data: managed servers, fallback pairs, approval source.

    Only values live here. The hash/scope/pin checks, the health gate
    and the unmanaged-server passthrough stay in the shared logic below.
    """

    vendor_id: str
    managed_server_ids: frozenset[str]
    #: Managed server -> primary peer server whose health decides fallback.
    fallback_map: dict[str, str]
    #: Engagement-metadata key holding the approved-hash mapping.
    approval_ref: str = "mobsf_agent"
    version: str = "1"
    #: Servers whose tools require an approved-hash opt-in plus exact health.
    #: The default preserves the built-in single-vendor behaviour.
    hash_required_server_ids: frozenset[str] = frozenset({"mobsf"})


BUILTIN_MOBILE_VENDOR_POLICY = VendorPolicy(
    vendor_id="builtin-mobile",
    managed_server_ids=frozenset({"mobsf", "analysis-local", "mobile-local"}),
    fallback_map={"analysis-local": "apktool", "mobile-local": "mobile-next"},
)


def builtin_mobile_vendor_policies() -> tuple[VendorPolicy, ...]:
    return (BUILTIN_MOBILE_VENDOR_POLICY,)


def _build_server_policy_map(
    policies: tuple[VendorPolicy, ...],
) -> dict[str, VendorPolicy]:
    """Map each managed server to its single owning policy.

    Raises ValueError when two policies claim the same server, naming
    the server id and both vendor ids.
    """
    owner: dict[str, VendorPolicy] = {}
    for policy in policies:
        for server_id in policy.managed_server_ids:
            existing = owner.get(server_id)
            if existing is not None:
                raise ValueError(
                    f"duplicate managed server '{server_id}' owned by vendor "
                    f"policies '{existing.vendor_id}' and '{policy.vendor_id}'"
                )
            owner[server_id] = policy
    return owner


class MobSFAgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: StrictBool = False
    assets: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_hashes(self):
        import re

        if self.enabled and not self.assets:
            raise ValueError("mobsf_agent_requires_approved_hashes")
        if any(not re.fullmatch(r"[a-f0-9]{64}", value) for value in self.assets.values()):
            raise ValueError("mobsf_agent_requires_approved_hashes")
        return self


class MobileMCPEligibility:
    """Filter planning AND execution with current health, retaining raw discovery."""

    def __init__(
        self,
        catalog,
        gateway,
        engagement,
        *,
        vendor_policies: Iterable[VendorPolicy] | None = None,
    ):
        self.catalog = catalog
        self.gateway = gateway
        self.engagement = engagement
        self.vendor_policies = (
            tuple(vendor_policies)
            if vendor_policies is not None
            else builtin_mobile_vendor_policies()
        )
        self._server_policy = _build_server_policy_map(self.vendor_policies)
        policy_configs: dict[str, MobSFAgentConfig] = {}
        server_configs: dict[str, MobSFAgentConfig] = {}
        for policy in self.vendor_policies:
            raw = engagement.metadata.get(policy.approval_ref, {}) if engagement else {}
            cfg = MobSFAgentConfig.model_validate(raw)
            if cfg.enabled and (
                engagement is None
                or set(cfg.assets) - set(engagement.scope.allowed_assets)
                or set(cfg.assets) & set(engagement.scope.denied_assets)
            ):
                raise ValueError("mobsf_agent_assets_outside_scope")
            policy_configs[policy.vendor_id] = cfg
            for server_id in policy.managed_server_ids:
                server_configs[server_id] = cfg
        self._policy_configs = policy_configs
        self._server_configs = server_configs
        if self.vendor_policies:
            self.config = policy_configs[self.vendor_policies[0].vendor_id]
        else:
            self.config = MobSFAgentConfig.model_validate({})
        # Flat views kept for compatibility; judgments use _server_policy.
        self.managed_server_ids = frozenset(self._server_policy.keys())
        flat_fallback: dict[str, str] = {}
        for policy in self.vendor_policies:
            flat_fallback.update(policy.fallback_map)
        self.fallback_map = flat_fallback

    def __getattr__(self, name):
        return getattr(self.gateway, name)

    def eligible(self, tool) -> bool:
        server_id = getattr(tool, "server_id", None)
        if server_id is None:
            return True
        owner = self._server_policy.get(server_id)
        if owner is None:
            return True
        cfg = self._server_configs[server_id]
        if server_id in owner.hash_required_server_ids:
            return bool(cfg.enabled) and tool.health_status is ToolHealthStatus.HEALTHY
        primary = owner.fallback_map.get(server_id)
        if primary is None:
            return True
        if tool.health_status is not ToolHealthStatus.HEALTHY:
            return False
        # Absent or UNKNOWN primary tools do not prove an all-unhealthy condition.
        tags = getattr(tool, "capability_tags", None) or []
        peers_by_id = {}
        for tag in tags:
            for peer in self.catalog.list_by_capability(tag):
                if getattr(peer, "server_id", None) == primary:
                    peers_by_id[getattr(peer, "tool_id", id(peer))] = peer
        peers = list(peers_by_id.values())
        return bool(peers) and all(t.health_status is ToolHealthStatus.UNHEALTHY for t in peers)

    def get(self, tool_id):
        tool = self.catalog.get(tool_id)
        return tool if tool is not None and self.eligible(tool) else None

    def list_by_capability(self, capability_tag):
        return [t for t in self.catalog.list_by_capability(capability_tag) if self.eligible(t)]

    def list_tools_for_action(self, capability, operation):
        return [
            t for t in self.catalog.list_tools_for_action(capability, operation) if self.eligible(t)
        ]

    async def invoke(self, invocation):
        return await self._invoke(invocation)

    async def invoke_with_admission(self, invocation, before_start: BeforeToolStart):
        return await self._invoke(invocation, before_start=before_start)

    async def _invoke(self, invocation, *, before_start=None):
        raw = self.catalog.get(invocation.tool_id)
        raw_server = getattr(raw, "server_id", None) if raw is not None else None
        owner = self._server_policy.get(raw_server) if raw is not None else None
        if owner is not None:
            refresh = getattr(self.gateway, "refresh_health", None)
            if refresh is not None:
                await refresh(force=True)
            raw = self.catalog.get(invocation.tool_id)
            allowed = raw is not None and self.eligible(raw)
            current_owner = self._server_policy.get(raw.server_id) if raw is not None else None
            if (
                raw is not None
                and current_owner is not None
                and raw.server_id in current_owner.hash_required_server_ids
            ):
                cfg = self._server_configs[raw.server_id]
                ref = invocation.arguments.get("apk_ref")
                pinned_hash = cfg.assets.get(ref)
                allowed = (
                    allowed
                    and pinned_hash is not None
                    and (invocation.arguments.get("expected_sha256", pinned_hash) == pinned_hash)
                    and self.engagement is not None
                    and (
                        invocation.execution_context.engagement_id == self.engagement.engagement_id
                    )
                )
            if not allowed:
                now = datetime.now(timezone.utc)
                return ToolResult(
                    invocation_id=invocation.invocation_id,
                    action_id=invocation.action_id,
                    tool_id=invocation.tool_id,
                    success=False,
                    started_at=now,
                    finished_at=now,
                    error=ToolError(
                        code="mobile_mcp_ineligible", message="MCP eligibility gate denied"
                    ),
                )
        if before_start is not None:
            if isinstance(self.gateway, ToolStartAdmissionGateway):
                return await self.gateway.invoke_with_admission(invocation, before_start)
            await before_start()
        return await self.gateway.invoke(invocation)


def wire_mobile_mcp(
    deps, engagement=None, *, vendor_policies: Iterable[VendorPolicy] | None = None
):
    """Inject a run-local view using existing ports; no central engine changes."""
    policies = (
        tuple(vendor_policies) if vendor_policies is not None else builtin_mobile_vendor_policies()
    )
    server_policy = _build_server_policy_map(policies)
    if deps.tool_catalog is None or isinstance(deps.tool_catalog, MobileMCPEligibility):
        return deps
    capabilities: set[str] = {"analysis", "mobile"}
    agents = getattr(deps, "agents", None)
    if agents is not None:
        capabilities.update(cap for agent in agents.all() for cap in agent.capabilities)
    tools_by_id = {}
    for capability in capabilities:
        for tool in deps.tool_catalog.list_by_capability(capability):
            tools_by_id[getattr(tool, "tool_id", id(tool))] = tool
    tools = list(tools_by_id.values())
    if not any(getattr(tool, "server_id", None) in server_policy for tool in tools):
        return deps
    gate = MobileMCPEligibility(
        deps.tool_catalog,
        deps.tool_gateway,
        engagement or deps.engagement,
        vendor_policies=policies,
    )
    package = mobsf_observation_package()
    if any(
        tool.server_id == "mobsf" and tool.tool_name == "mobsf_report_summary" for tool in tools
    ):
        if not deps.result_mappers.has_mapper(package.tool_ids[0]):
            register_contribution(deps.result_mappers, package)
        deps.result_mappers.bind_catalog(deps.tool_catalog)
    return replace(deps, tool_catalog=gate, tool_gateway=gate)


def mobile_vendor_policy_contributions(
    policies: Iterable[VendorPolicy] | None = None,
) -> tuple:
    """Vendor data in the composition root's declaration shape (M04)."""
    from redteam.core.extensions import Contribution, value_factory

    selected = tuple(policies) if policies is not None else builtin_mobile_vendor_policies()
    return tuple(
        Contribution(
            kind=VENDOR_POLICY_KIND,
            contribution_id=policy.vendor_id,
            version=policy.version,
            factory=value_factory(policy),
        )
        for policy in selected
    )


def builtin_extension_declaration():
    """Built-in vendor data as one declaration for the composition root."""
    from redteam.core.extensions import ExtensionDeclaration

    return ExtensionDeclaration(
        extension_id="builtin.mobile-vendors",
        version="1",
        contributions=mobile_vendor_policy_contributions(),
    )
