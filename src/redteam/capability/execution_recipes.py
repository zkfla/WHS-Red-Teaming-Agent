"""Provider-owned execution recipes (fixed command bindings).

The bridge used to hardcode one recipe (line_count over wc -l with a ten
second timeout) plus the four command operations. The recipe rows live
here; the bridge consumes them generically and keeps every gate and
error message. Tables may be overridden per call so the composition
root can bind assembly-provided recipes later without touching callers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

from redteam.core.provider_contracts import ExecutionRecipeSpec

BUILTIN_EXECUTION_RECIPES: tuple[ExecutionRecipeSpec, ...] = (
    ExecutionRecipeSpec(
        name="line_count",
        capability="analysis",
        operation="command_run",
        tool_name="run_command",
        executable="wc",
        executable_args=("-l",),
        timeout_seconds=10.0,
    ),
)


def builtin_execution_recipes() -> dict[str, ExecutionRecipeSpec]:
    """Fresh name-to-recipe table of the builtin rows."""

    return {spec.name: spec for spec in BUILTIN_EXECUTION_RECIPES}


KIND_EXECUTION_RECIPE = "capability.execution_recipe"


def extension_contributions() -> tuple:
    """Builtin recipes as extension contributions."""

    from redteam.core.extensions import Contribution, value_factory

    return tuple(
        Contribution(
            kind=KIND_EXECUTION_RECIPE,
            contribution_id="recipe:" + spec.name,
            version="1",
            factory=value_factory(spec),
        )
        for spec in BUILTIN_EXECUTION_RECIPES
    )
