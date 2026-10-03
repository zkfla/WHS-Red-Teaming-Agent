"""MobSF adapter with a real REST health probe and trusted Gateway context binding."""

from __future__ import annotations

from redteam.capability.adapters.mcp.scoped_analysis import ScopedAnalysisMCPAdapter
from redteam.capability.servers.mobsf import MobSFClient, MobSFPolicy
from redteam.capability.settings import MCPConfigurationError
from redteam.core.enums import ToolHealthStatus


class MobSFMCPAdapter(ScopedAnalysisMCPAdapter):
    error_prefix = "mobsf"

    @classmethod
    def from_settings(cls, settings, transport):
        try:
            policy_path = settings.args[settings.args.index("--policy") + 1]
            digest = settings.args[settings.args.index("--sha256") + 1]
            policy = MobSFPolicy.load(policy_path, digest)
        except (ValueError, IndexError, OSError):
            raise MCPConfigurationError("mobsf_pinned_policy_required") from None
        adapter = cls(
            server_id=settings.server_id,
            transport=transport,
            risk_level=settings.risk_level,
        )
        adapter.client = MobSFClient(policy)
        return adapter

    async def health_check(self) -> ToolHealthStatus:
        healthy = await self.client.healthy() and await self._transport.health_check()
        return ToolHealthStatus.HEALTHY if healthy else ToolHealthStatus.UNHEALTHY
