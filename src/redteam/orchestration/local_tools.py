"""General runtime-local tool registry and contribution contracts (B0).

A runtime-local tool runs inside the execution context, not against a
remote target server. It spends the caller reasoning budget, never the
target action budget, and its result travels back through the generic
model tool_calls path, so the center never branches on tool names.

Wiring into the assembly root stays root-owned; this module only
declares the seam. Shared-contract addition (the runtime_tool kind)
needs root sign-off before dispatch.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, Field

from redteam.core.extensions import Contribution
from redteam.orchestration.cognition import DecisionBudget

RUNTIME_TOOL_KIND = "runtime_tool"
RUNTIME_TOOL_CONTRIBUTION_VERSION = "v1"


class LocalToolCall(BaseModel):
    tool_id: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class LocalToolContext:
    """What a local handler may consume. No engine dependency."""

    deps: Any
    runtime: Any
    purpose: str = ""
    path: Any | None = None
    delegation: Any | None = None
    budget: DecisionBudget | None = None
    before_call: Callable[[], Any] | None = None


@dataclass(frozen=True)
class LocalToolDescriptor:
    """Exposed under local_tools, apart from remote target tools."""

    tool_id: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    contribution_id: str = ""
    version: str = RUNTIME_TOOL_CONTRIBUTION_VERSION


@dataclass(frozen=True)
class LocalToolResult:
    """Generic payload for the next-turn tool_calls delivery."""

    ok: bool
    payload: dict[str, Any] = field(default_factory=dict)
    refs: tuple[str, ...] = ()
    error: str | None = None
    local_call_id: str = ""


class LocalToolHandler(Protocol):
    async def __call__(
        self, tool_input: dict[str, Any], context: LocalToolContext
    ) -> LocalToolResult: ...


LocalToolFactory = Callable[[Any], tuple[LocalToolDescriptor, LocalToolHandler]]


_SIMPLE_SCHEMA_TYPES: dict[str, Any] = {
    "string": str,
    "boolean": bool,
    "array": list,
    "object": dict,
    "integer": int,
    "number": (int, float),
}


def _schema_problem(schema: Any, tool_input: Any) -> str | None:
    """Generic shape check. Masked wording: names only, never values."""
    if not isinstance(tool_input, dict):
        return "bad local input"
    if not isinstance(schema, dict):
        return None
    for name in schema.get("required") or []:
        if str(name) not in tool_input:
            return f"missing required field: {name}"
    properties = schema.get("properties") or {}
    if not isinstance(properties, dict):
        return None
    for name, spec in properties.items():
        if str(name) not in tool_input or not isinstance(spec, dict):
            continue
        want = spec.get("type")
        if want not in _SIMPLE_SCHEMA_TYPES:
            continue
        value = tool_input[str(name)]
        if isinstance(value, bool) and want != "boolean":
            return f"bad field type: {name}"
        if not isinstance(value, _SIMPLE_SCHEMA_TYPES[want]):
            return f"bad field type: {name}"
    return None


class LocalToolRegistry:
    """Dict-dispatched async registry. No per-tool central branching."""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[LocalToolDescriptor, LocalToolHandler]] = {}

    def register(
        self, descriptor: LocalToolDescriptor, handler: LocalToolHandler
    ) -> None:
        key = descriptor.tool_id.strip()
        if not key:
            raise ValueError("local tool id must not be blank")
        if key in self._entries:
            raise ValueError(f"local tool already registered: {key}")
        self._entries[key] = (descriptor, handler)

    def get(self, tool_id: str) -> LocalToolHandler | None:
        entry = self._entries.get(str(tool_id).strip())
        return entry[1] if entry is not None else None

    def descriptors(self) -> tuple[LocalToolDescriptor, ...]:
        """Local-only listing, apart from the remote target catalog."""
        return tuple(entry[0] for entry in self._entries.values())

    def copy(self) -> LocalToolRegistry:
        """Assembly may add selected features without mutating the source component set."""
        registry = LocalToolRegistry()
        registry._entries = dict(self._entries)
        return registry

    async def call(
        self,
        tool_id: str,
        tool_input: dict[str, Any],
        context: LocalToolContext,
    ) -> LocalToolResult:
        key = str(tool_id).strip()
        entry = self._entries.get(key)
        if entry is None:
            return LocalToolResult(ok=False, error="unknown local tool")
        descriptor, handler = entry
        problem = _schema_problem(descriptor.input_schema, tool_input)
        if problem is not None:
            return LocalToolResult(ok=False, error=problem)
        return await handler(dict(tool_input or {}), context)


def runtime_tool_contribution(
    *,
    contribution_id: str,
    version: str = RUNTIME_TOOL_CONTRIBUTION_VERSION,
    factory: LocalToolFactory,
) -> Contribution:
    """Declare one runtime-local tool for the pluggy assembly."""
    if not contribution_id.strip():
        raise ValueError("contribution id must not be blank")
    return Contribution(
        kind=RUNTIME_TOOL_KIND,
        contribution_id=contribution_id.strip(),
        version=version,
        factory=factory,
    )
