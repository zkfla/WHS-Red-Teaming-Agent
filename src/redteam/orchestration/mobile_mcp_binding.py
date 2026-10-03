"""Run-local MCP eligibility view; discovery health is never rewritten as policy."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from redteam.core.contracts.tool import ToolError, ToolResult
from redteam.core.enums import ToolHealthStatus
from redteam.orchestration.observations.mobsf import map_mobsf_report


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

    def __init__(self, catalog, gateway, engagement):
        self.catalog = catalog
        self.gateway = gateway
        self.engagement = engagement
        raw = engagement.metadata.get("mobsf_agent", {}) if engagement else {}
        self.config = MobSFAgentConfig.model_validate(raw)
        if self.config.enabled and (
            engagement is None
            or set(self.config.assets) - set(engagement.scope.allowed_assets)
            or set(self.config.assets) & set(engagement.scope.denied_assets)
        ):
            raise ValueError("mobsf_agent_assets_outside_scope")

    def __getattr__(self, name):
        return getattr(self.gateway, name)

    def eligible(self, tool) -> bool:
        if tool.server_id == "mobsf":
            return self.config.enabled and tool.health_status is ToolHealthStatus.HEALTHY
        primary = {"analysis-local": "apktool", "mobile-local": "mobile-next"}.get(tool.server_id)
        if primary is None:
            return True
        if tool.health_status is not ToolHealthStatus.HEALTHY:
            return False
        # Absent or UNKNOWN primary tools do not prove an all-unhealthy condition.
        peers = [
            t
            for t in self.catalog.list_by_capability(
                "analysis" if primary == "apktool" else "mobile"
            )
            if t.server_id == primary
        ]
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
        raw = self.catalog.get(invocation.tool_id)
        managed = raw is not None and raw.server_id in {"mobsf", "analysis-local", "mobile-local"}
        if managed:
            refresh = getattr(self.gateway, "refresh_health", None)
            if refresh is not None:
                await refresh(force=True)
            raw = self.catalog.get(invocation.tool_id)
            allowed = raw is not None and self.eligible(raw)
            if raw is not None and raw.server_id == "mobsf":
                ref = invocation.arguments.get("apk_ref")
                pinned_hash = self.config.assets.get(ref)
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
        return await self.gateway.invoke(invocation)


def wire_mobile_mcp(deps, engagement=None):
    """Inject a run-local view using existing ports; no central engine changes."""
    if deps.tool_catalog is None or isinstance(deps.tool_catalog, MobileMCPEligibility):
        return deps
    tools = [
        *deps.tool_catalog.list_by_capability("analysis"),
        *deps.tool_catalog.list_by_capability("mobile"),
    ]
    if not any(t.server_id in {"mobsf", "analysis-local", "mobile-local"} for t in tools):
        return deps
    gate = MobileMCPEligibility(deps.tool_catalog, deps.tool_gateway, engagement or deps.engagement)
    if any(t.server_id == "mobsf" for t in tools):
        deps.result_mappers.register("analysis.mobsf_report_summary", map_mobsf_report)
    return replace(deps, tool_catalog=gate, tool_gateway=gate)
