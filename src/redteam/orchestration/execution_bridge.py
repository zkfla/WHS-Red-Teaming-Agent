"""Narrow, opt-in bridge from the Analysis Agent to execution MCP tools.

This is intentionally a temporary integration seam.  The Agent team owns the
future workflow; this module only exposes one code-owned text-processing recipe
to the existing LLM-led path/action pipeline.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from redteam.attack_graph.identity import asset_node_id
from redteam.attack_graph.predicates import facts_of
from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.engagement import Engagement
from redteam.core.contracts.graph import GraphNode
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.enums import GraphNodeType
from redteam.core.ids import RunId

EXECUTION_OPERATIONS = frozenset(
    {"command_run", "command_job_start", "command_job_status", "command_job_cancel"}
)
EXECUTION_RECIPE_FACT = "execution_recipe"
EXECUTION_PATH_FACT = "execution_workspace_path"


class ExecutionAssetRecipe(BaseModel):
    """One operator-selected recipe bound to an approved workspace path."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    recipe: Literal["line_count"]
    path: str = Field(min_length=1, max_length=1024)

    @field_validator("path")
    @classmethod
    def _relative_workspace_path(cls, value: str) -> str:
        if (
            value != value.strip()
            or value.startswith("/")
            or "\\" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
        ):
            raise ValueError("execution Agent path must be a normalized relative POSIX path")
        return value


class ExecutionAgentConfig(BaseModel):
    """Operator-owned feature switch carried by ``Engagement.metadata``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: StrictBool = False
    assets: dict[str, ExecutionAssetRecipe] = Field(default_factory=dict)

    @field_validator("assets")
    @classmethod
    def _asset_names_are_opaque(cls, value: dict[str, ExecutionAssetRecipe]):
        if any(not key or key != key.strip() for key in value):
            raise ValueError("execution Agent asset keys must be non-empty and normalized")
        return value

    @model_validator(mode="after")
    def _enabled_configuration_has_work(self):
        if self.enabled and not self.assets:
            raise ValueError("enabled execution Agent requires at least one asset recipe")
        return self


def execution_agent_config(engagement: Engagement | None) -> ExecutionAgentConfig:
    if engagement is None:
        return ExecutionAgentConfig()
    raw = engagement.metadata.get("execution_agent")
    if raw is None:
        return ExecutionAgentConfig()
    if not isinstance(raw, dict):
        raise ValueError("engagement.metadata.execution_agent must be an object")
    return ExecutionAgentConfig.model_validate(raw)


def execution_facts_by_asset(
    engagement: Engagement,
    declared_assets: list[str],
) -> dict[str, dict[str, str]]:
    """Validate opt-in asset bindings and return graph declaration facts."""

    config = execution_agent_config(engagement)
    if not config.enabled:
        return {}
    unknown = set(config.assets) - set(declared_assets)
    if unknown:
        raise ValueError("execution Agent assets must be in engagement scope")
    return {
        asset: {
            EXECUTION_RECIPE_FACT: binding.recipe,
            EXECUTION_PATH_FACT: binding.path,
        }
        for asset, binding in config.assets.items()
    }


def is_execution_tool(tool: ToolDescriptor) -> bool:
    return bool(EXECUTION_OPERATIONS.intersection(tool.operations))


def agent_tool_visible(tool: ToolDescriptor, *, execution_ready: bool) -> bool:
    """Hide every command operation unless a graph-bound recipe is ready."""

    if not is_execution_tool(tool):
        return True
    return (
        execution_ready and tool.operations == ["command_run"] and tool.tool_name == "run_command"
    )


def _arguments_for_facts(
    facts: dict[str, Any],
    *,
    engagement_id: str,
    run_id: str,
) -> dict[str, Any]:
    recipe = facts.get(EXECUTION_RECIPE_FACT)
    path = facts.get(EXECUTION_PATH_FACT)
    asset_ref = facts.get("asset_ref")
    if recipe != "line_count" or not isinstance(path, str) or not isinstance(asset_ref, str):
        raise ValueError("execution Agent target has no configured graph binding")
    # executable and options are code-owned; only the validated relative path
    # and operator-declared asset identity come from graph facts.
    return {
        "asset_ref": asset_ref,
        "executable": "wc",
        "args": ["-l", path],
        "timeout_seconds": 10.0,
        "engagement_id": engagement_id,
        "run_id": run_id,
    }


def _configured_arguments(engagement: Engagement, run_id: RunId, node: GraphNode) -> dict[str, Any]:
    config = execution_agent_config(engagement)
    # Graph facts describe the binding; current operator configuration grants
    # it. A later graph update must not select a new path or asset.
    assets = {asset.strip() for asset in engagement.scope.allowed_assets} - {
        asset.strip() for asset in engagement.scope.denied_assets
    }
    asset = next((name for name in config.assets if asset_node_id(name) == node.id), None)
    if not config.enabled or asset is None or asset not in assets:
        raise ValueError("execution Agent target is not currently configured in scope")
    binding = config.assets[asset]
    facts = facts_of(node.properties)
    if (
        facts.get(EXECUTION_RECIPE_FACT) != binding.recipe
        or facts.get(EXECUTION_PATH_FACT) != binding.path
    ):
        raise ValueError("execution Agent graph binding differs from current configuration")
    raw_refs = engagement.metadata.get("asset_refs", {})
    expected_ref = raw_refs.get(asset, asset) if isinstance(raw_refs, dict) else asset
    if len(assets) == 1 and engagement.metadata.get("asset_ref") is not None:
        expected_ref = engagement.metadata["asset_ref"]
    if facts.get("asset_ref") != str(expected_ref).strip():
        raise ValueError("execution Agent asset reference differs from current configuration")
    return _arguments_for_facts(
        facts, engagement_id=str(engagement.engagement_id), run_id=str(run_id)
    )


async def execution_agent_options(
    service: AttackGraphService,
    engagement: Engagement | None,
    run_id: RunId,
) -> list[dict[str, Any]]:
    """Return fixed action options only for configured, seeded Asset nodes."""

    config = execution_agent_config(engagement)
    if engagement is None or not config.enabled:
        return []
    options: list[dict[str, Any]] = []
    for asset in sorted(config.assets):
        node = await service.get_node(asset_node_id(asset))
        if node is None or node.type is not GraphNodeType.ASSET:
            continue
        try:
            arguments = _configured_arguments(engagement, run_id, node)
        except ValueError:
            continue
        options.append(
            {
                "agent_id": "analysis-agent",
                "target_node_id": str(node.id),
                "capability": "analysis",
                "operation": "command_run",
                "tool_id": "analysis.run_command",
                "arguments": arguments,
            }
        )
    return options


def bind_execution_arguments(
    *,
    engagement: Engagement | None,
    run_id: RunId,
    node: GraphNode,
    tool: ToolDescriptor,
    capability: str,
    operation: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Replace model-supplied command arguments with the graph-bound recipe."""

    if operation not in EXECUTION_OPERATIONS:
        return arguments
    config = execution_agent_config(engagement)
    if engagement is None or not config.enabled:
        raise ValueError("execution Agent is not enabled for this engagement")
    if (
        capability != "analysis"
        or operation != "command_run"
        or tool.tool_name != "run_command"
        or tool.operations != ["command_run"]
        or node.type is not GraphNodeType.ASSET
    ):
        raise ValueError("execution Agent only permits the fixed command_run recipe")
    return _configured_arguments(engagement, run_id, node)
