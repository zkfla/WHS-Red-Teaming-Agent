"""Pure role-selection contract and predicate (no file IO, no catalog)."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field


class RoleToolSelection(BaseModel):
    """One role's tool selectors. Methodology refs never filter tools."""

    model_config = ConfigDict(extra="forbid")
    agent_id: str = Field(min_length=1)
    tool_ids: list[str] = Field(default_factory=list)
    server_ids: list[str] = Field(default_factory=list)
    capability_tags: list[str] = Field(default_factory=list)
    methodology_refs: list[str] = Field(default_factory=list)


def select_for_role(manifest, agent_id):
    """Pure row lookup on an already-loaded manifest. Never reads files."""

    for role in manifest.roles:
        if role.agent_id == agent_id:
            return role
    return None


def coerce_selection(agent_id, inline):
    """Normalize an injected snapshot into a RoleToolSelection."""

    if isinstance(inline, RoleToolSelection):
        if str(inline.agent_id) != str(agent_id):
            raise ValueError("inline selection agent_id does not match the definition")
        return inline
    if not isinstance(inline, Mapping):
        raise ValueError("AgentDefinition.tool_selection must be a mapping")
    if "agent_id" in inline and str(inline["agent_id"]) != str(agent_id):
        raise ValueError("inline selection agent_id does not match the definition")
    data = {key: inline[key] for key in inline if key != "agent_id"}
    return RoleToolSelection(agent_id=str(agent_id), **data)


def fallback_from_capabilities(agent):
    """Default selection from the definition's own capabilities."""

    tags = sorted(
        {str(tag) for tag in (getattr(agent, "capabilities", None) or ()) if str(tag)}
    )
    if not tags:
        return None
    return RoleToolSelection(
        agent_id=str(getattr(agent, "agent_id", "")), capability_tags=tags
    )


def selection_allows(selection, tool_id="", server_id="", capability_tags=()):
    """Whether one tool passes a resolved role selection."""

    if selection is None:
        return True
    if not (selection.tool_ids or selection.server_ids or selection.capability_tags):
        return False
    if selection.tool_ids and str(tool_id) not in selection.tool_ids:
        return False
    if selection.server_ids and str(server_id) not in selection.server_ids:
        return False
    if selection.capability_tags:
        wanted = [str(tag) for tag in capability_tags]
        if not any(tag in selection.capability_tags for tag in wanted):
            return False
    return True


def resolve_injected_selection(definition, manifest=None):
    """Resolve from the assembled snapshot only. Never reads files."""

    inline = getattr(definition, "tool_selection", None)
    if inline is not None:
        return coerce_selection(str(getattr(definition, "agent_id", "")), inline)
    if manifest is not None:
        row = select_for_role(manifest, str(getattr(definition, "agent_id", "")))
        if row is not None:
            return row
    return fallback_from_capabilities(definition)
