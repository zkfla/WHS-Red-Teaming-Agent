"""One specialist-role view for cards, details, native binding and launch.

Catalog/eligibility are the sources. This grants no execution permission;
the existing action admission and Control still recheck every request.
"""

from redteam.attack_graph.service import AttackGraphService
from redteam.core.enums import ToolHealthStatus
from redteam.core.ids import AgentId
from redteam.orchestration.agents.role_view import tools_for_role
from redteam.orchestration.eligibility import EligibilityContext
from redteam.orchestration.local_tools import LocalToolDescriptor, LocalToolResult


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


async def role_readiness(deps) -> list[dict]:
    """Metadata/eligibility projection only, never live device/app attestation.

    UNKNOWN health may stay eligible under the existing dev contract but
    does not prove observed health. Do not silently rewrite that contract.
    Use the actual assembled catalog, not a vendor list.
    Every target call still needs target binding, schema and Control checks.
    """
    rows = []
    for role in sorted(deps.agents.all(), key=lambda item: str(item.agent_id)):
        identity = str(role.agent_id)
        visible = await eligible_role_tool_ids(deps, identity, include_unhealthy=True)
        eligible = await eligible_role_tool_ids(deps, identity)
        healthy = []
        for tool_id in sorted(eligible):
            tool = deps.tool_catalog.get(tool_id)
            if tool is not None and tool.health_status is ToolHealthStatus.HEALTHY:
                healthy.append(tool_id)
        rows.append({
            "role_id": identity, "catalog_ready": bool(healthy),
            "activation_eligible": bool(eligible),
            "reason_code": "catalog_ready" if healthy else (
                "no_healthy_eligible_tool" if visible else "no_eligible_tool_or_target"),
            "healthy_tool_ids": healthy, "visible_tool_ids": sorted(visible),
            "eligible_tool_ids": sorted(eligible),
            "target_execution_verified": False, "execution_permission_granted": False,
        })
    return rows


async def _readiness_handler(tool_input, context):
    from redteam.orchestration.tool_discovery import program_execution_environment

    rows = await role_readiness(context.deps)
    return LocalToolResult(ok=True, payload={
        "roles": rows, "target_execution_verified": False,
        "program_execution_environment": program_execution_environment(
            getattr(context.deps, "shell_programs", None),
            {identity for row in rows for identity in row["eligible_tool_ids"]},
        ),
    })


def register_role_readiness(registry) -> None:
    registry.register(LocalToolDescriptor(
        tool_id="agent.readiness", contribution_id="specialists:role-readiness", version="2",
        description="Read current role catalog/health eligibility. No device probing, target "
                    "execution, permission or readiness fabrication.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    ), _readiness_handler)
