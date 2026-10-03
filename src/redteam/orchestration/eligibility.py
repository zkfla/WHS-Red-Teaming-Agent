"""Common eligibility contract for role/tool visibility (M03)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from redteam.core.ports.tool_catalog import ToolCatalog

#: Contribution kind spoken by eligibility providers. The composition
#: root owns collection; the name is the draft so both sides can meet.
ELIGIBILITY_PROVIDER_KIND = "eligibility_provider"


@dataclass(frozen=True)
class EligibilityScope:
    """Which roles/tools one eligibility provider applies to."""

    capabilities: tuple[str, ...] = ()
    operations: tuple[str, ...] = ()
    server_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class EligibilityDecision:
    eligible: bool
    reason: str


@dataclass(frozen=True)
class EligibilityContext:
    """Live inputs one eligibility check reads; never stored."""

    graph: Any = None
    catalog: ToolCatalog | None = None
    execution_ready: bool = False


class EligibilityProvider(Protocol):
    """One domain's visibility rules (M03). Visibility only, never permission.

    All three methods are async: scope reads the live graph and health
    may be refreshed between the plan-time check and the execution-time
    re-check, so even cheap predicates stay awaitable.
    """

    @property
    def provider_id(self) -> str: ...

    @property
    def applies_to(self) -> EligibilityScope: ...

    async def role_eligible(
        self, role_id: str, context: EligibilityContext
    ) -> EligibilityDecision: ...

    async def tool_eligible(
        self, tool: Any, context: EligibilityContext
    ) -> EligibilityDecision: ...

    async def asset_scope(self, context: EligibilityContext) -> tuple[str, ...]: ...


class EligibilityRegistry:
    """Selected domain predicates; the central loop has no vendor branches."""

    def __init__(self, providers: Iterable[EligibilityProvider] = ()) -> None:
        self._providers: dict[str, EligibilityProvider] = {}
        for provider in providers:
            self.register(provider)

    def register(self, provider: EligibilityProvider) -> None:
        if provider.provider_id in self._providers:
            raise ValueError(f"duplicate eligibility provider: {provider.provider_id}")
        self._providers[provider.provider_id] = provider

    @property
    def provider_ids(self) -> tuple[str, ...]:
        return tuple(self._providers)

    def _for_capabilities(self, capabilities):
        return [
            provider
            for provider in self._providers.values()
            if not provider.applies_to.capabilities
            or set(provider.applies_to.capabilities).intersection(capabilities)
        ]

    def _for_tool(self, tool):
        return [
            provider
            for provider in self._for_capabilities(tool.capability_tags)
            if (
                not provider.applies_to.operations
                or set(provider.applies_to.operations).intersection(tool.operations)
            )
            and (
                not provider.applies_to.server_ids
                or tool.server_id in provider.applies_to.server_ids
            )
        ]

    async def role(self, agent, context):
        for provider in self._for_capabilities(agent.capabilities):
            decision = await provider.role_eligible(str(agent.agent_id), context)
            if not decision.eligible:
                return decision
        return EligibilityDecision(True, "eligible")

    async def tool(self, tool, context):
        for provider in self._for_tool(tool):
            decision = await provider.tool_eligible(tool, context)
            if not decision.eligible:
                return decision
        return EligibilityDecision(True, "eligible")

    async def action(self, agent, tool, target_node_id, context):
        for provider in self._for_tool(tool):
            if agent is not None:
                role = await provider.role_eligible(str(agent.agent_id), context)
                if not role.eligible:
                    return role
            result = await provider.tool_eligible(tool, context)
            if not result.eligible:
                return result
            targets = await provider.asset_scope(context)
            if targets and str(target_node_id) not in targets:
                return EligibilityDecision(False, "target_outside_provider_scope")
        return EligibilityDecision(True, "eligible")


def default_eligibility_registry():
    from redteam.orchestration.analysis_activation import StaticAnalysisEligibilityProvider

    return EligibilityRegistry((StaticAnalysisEligibilityProvider(),))
