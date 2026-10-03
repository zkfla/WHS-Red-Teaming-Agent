"""Narrow, opt-in bridge from analysis execution to command MCP tools.

Recipes (fixed command bindings) live in the capability-owned table and are
selected per call; this module only validates the operator-selected,
graph-bound recipe and substitutes its fixed arguments. No model-supplied
command, executable, or path ever reaches the gateway.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from redteam.attack_graph.identity import asset_node_id
from redteam.attack_graph.predicates import facts_of
from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.engagement import Engagement
from redteam.core.contracts.graph import GraphNode
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.enums import GraphNodeType
from redteam.core.ids import RunId
from redteam.core.provider_contracts import ExecutionRecipeSpec


def _execution_operations(recipes: Mapping[str, ExecutionRecipeSpec] | None) -> frozenset[str]:
    """Command lifecycle, plus every operation of the injected table.

    None keeps the legacy lifecycle set for direct default callers. An
    explicitly empty table stays empty here too: it is not the default,
    so new recipe operations only ever come from an injected table.
    """
    if recipes is None:
        return EXECUTION_OPERATIONS
    return frozenset(set(EXECUTION_OPERATIONS) | {row.operation for row in recipes.values()})


# Legacy alias kept for the call sites that still import it: the concrete
# membership is decided per call by _execution_operations, never by this set.
EXECUTION_OPERATIONS = frozenset(
    {"command_run", "command_job_start", "command_job_status", "command_job_cancel"}
)
EXECUTION_RECIPE_FACT = "execution_recipe"
EXECUTION_PATH_FACT = "execution_workspace_path"


def _recipe_row(recipes: Mapping[str, Any] | None, name: Any) -> Any | None:
    if recipes is None or not isinstance(name, str):
        return None
    return recipes.get(name)


def _row_identity(row: Any) -> tuple[str, str, str] | None:
    capability = getattr(row, "capability", None)
    operation = getattr(row, "operation", None)
    tool_name = getattr(row, "tool_name", None)
    if (
        not isinstance(capability, str)
        or not capability
        or not isinstance(operation, str)
        or not operation
        or not isinstance(tool_name, str)
        or not tool_name
    ):
        return None
    return (capability, operation, capability + "." + tool_name)


class ExecutionAssetRecipe(BaseModel):
    """One operator-selected recipe bound to an approved workspace path."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    recipe: str = Field(min_length=1, max_length=128)
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
    *,
    recipes: Mapping[str, ExecutionRecipeSpec] | None = None,
) -> dict[str, dict[str, str]]:
    """Validate opt-in asset bindings and return graph declaration facts."""

    config = execution_agent_config(engagement)
    if not config.enabled:
        return {}
    unknown = set(config.assets) - set(declared_assets)
    if unknown:
        raise ValueError("execution Agent assets must be in engagement scope")
    for binding in config.assets.values():
        if recipes is None:
            known = binding.recipe == "line_count"
        else:
            known = binding.recipe in recipes
        if not known:
            raise ValueError("execution Agent recipe is not in the selected recipe table")
    return {
        asset: {
            EXECUTION_RECIPE_FACT: binding.recipe,
            EXECUTION_PATH_FACT: binding.path,
        }
        for asset, binding in config.assets.items()
    }


def is_execution_tool(
    tool: ToolDescriptor,
    *,
    recipes: Mapping[str, ExecutionRecipeSpec] | None = None,
) -> bool:
    return bool(_execution_operations(recipes).intersection(tool.operations))


def agent_tool_visible(
    tool: ToolDescriptor,
    *,
    execution_ready: bool,
    recipes: Mapping[str, ExecutionRecipeSpec] | None = None,
) -> bool:
    """Hide every command operation unless a graph-bound recipe is ready."""

    if not is_execution_tool(tool, recipes=recipes):
        return True
    if recipes is None:
        return (
            execution_ready
            and tool.operations == ["command_run"]
            and tool.tool_name == "run_command"
        )
    return execution_ready and any(
        tool.operations == [row.operation] and tool.tool_name == row.tool_name
        for row in recipes.values()
    )


def _arguments_for_facts(
    facts: dict[str, Any],
    *,
    engagement_id: str,
    run_id: str,
    recipes: Mapping[str, ExecutionRecipeSpec] | None = None,
) -> dict[str, Any]:
    recipe = facts.get(EXECUTION_RECIPE_FACT)
    path = facts.get(EXECUTION_PATH_FACT)
    asset_ref = facts.get("asset_ref")
    if recipes is None:
        if recipe != "line_count" or not isinstance(path, str) or not isinstance(asset_ref, str):
            raise ValueError("execution Agent target has no configured graph binding")
        return {
            "asset_ref": asset_ref,
            "executable": "wc",
            "args": ["-l", path],
            "timeout_seconds": 10.0,
            "engagement_id": engagement_id,
            "run_id": run_id,
        }
    row = _recipe_row(recipes, recipe)
    if row is None or not isinstance(path, str) or not isinstance(asset_ref, str):
        raise ValueError("execution Agent target has no configured graph binding")
    return row.build_arguments(
        asset_ref=asset_ref,
        path=path,
        engagement_id=engagement_id,
        run_id=run_id,
    )


def _configured_arguments(
    engagement: Engagement,
    run_id: RunId,
    node: GraphNode,
    *,
    recipes: Mapping[str, ExecutionRecipeSpec] | None = None,
) -> dict[str, Any]:
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
        facts,
        engagement_id=str(engagement.engagement_id),
        run_id=str(run_id),
        recipes=recipes,
    )


async def execution_agent_options(
    service: AttackGraphService,
    engagement: Engagement | None,
    run_id: RunId,
    *,
    recipes: Mapping[str, ExecutionRecipeSpec] | None = None,
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
        identity: tuple[str, str, str] | None
        if recipes is None:
            identity = ("analysis", "command_run", "analysis.run_command")
        else:
            identity = _row_identity(_recipe_row(recipes, config.assets[asset].recipe))
            if identity is None:
                continue
        try:
            arguments = _configured_arguments(engagement, run_id, node, recipes=recipes)
        except ValueError:
            continue
        options.append(
            {
                "target_node_id": str(node.id),
                "capability": identity[0],
                "operation": identity[1],
                "tool_id": identity[2],
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
    recipes: Mapping[str, ExecutionRecipeSpec] | None = None,
) -> dict[str, Any]:
    """Replace model-supplied command arguments with the graph-bound recipe."""

    if operation not in _execution_operations(recipes):
        return arguments
    config = execution_agent_config(engagement)
    if engagement is None or not config.enabled:
        raise ValueError("execution Agent is not enabled for this engagement")
    if recipes is None:
        permitted = (
            capability == "analysis"
            and operation == "command_run"
            and tool.tool_name == "run_command"
            and tool.operations == ["command_run"]
        )
    else:
        row = _recipe_row(recipes, facts_of(node.properties).get(EXECUTION_RECIPE_FACT))
        permitted = (
            row is not None
            and capability == row.capability
            and operation == row.operation
            and tool.tool_name == row.tool_name
            and tool.operations == [row.operation]
        )
    if not permitted or node.type is not GraphNodeType.ASSET:
        raise ValueError("execution Agent only permits the fixed command_run recipe")
    return _configured_arguments(engagement, run_id, node, recipes=recipes)
