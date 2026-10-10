"""One specialist-role view for cards, details, native binding and launch.

Catalog/eligibility are the sources. This grants no execution permission;
the existing action admission and Control still recheck every request.
"""

from redteam.attack_graph.service import AttackGraphService
from redteam.core.enums import ToolHealthStatus
from redteam.core.ids import AgentId
from redteam.orchestration.agents.role_view import tools_for_role
from redteam.orchestration.eligibility import EligibilityContext


async def eligible_role_tool_ids(deps, role_id: str, *, include_unhealthy=False) -> set[str]:
    role = deps.agents.get(AgentId(role_id))
    definition = deps.agents.definition(AgentId(role_id))
    if role is None or definition is None or not definition.schedulable:
        return set()
    context = EligibilityContext(AttackGraphService(deps.graph_store), deps.tool_catalog)
    if not (await deps.eligibility.role(role, context)).eligible:
        return set()
    identities = set()
    for tool in tools_for_role(definition, deps.tool_catalog):
        if (include_unhealthy or tool.health_status is not ToolHealthStatus.UNHEALTHY) and (
            await deps.eligibility.tool(tool, context)
        ).eligible:
            identities.add(str(tool.tool_id))
    return identities


async def specialist_tool_ids(deps, runtime, *, include_unhealthy=False) -> set[str] | None:
    if getattr(deps, "specialist_policy", None) is None:
        return None  # Keep dev's generic union view, including read-only tools UI.
    role = getattr(runtime, "current_agent", None)
    if role is None:
        return set()
    return await eligible_role_tool_ids(deps, str(role), include_unhealthy=include_unhealthy)


async def ready_role_ids(deps) -> set[str]:
    return {str(role.agent_id) for role in deps.agents.all()
            if await eligible_role_tool_ids(deps, str(role.agent_id))}
