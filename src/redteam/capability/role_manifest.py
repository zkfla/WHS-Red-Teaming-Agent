# Selectors-only role manifest: selection data, never role definitions.
#
# Selector semantics (AND across dimensions, OR within one dimension):
# - tool_ids: membership, the tool itself must be listed.
# - server_ids: membership, the tool server must be listed.
# - capability_tags: overlap, at least one tool tag must be listed.
# - methodology_refs: methodology scope only, never filters tools.
# An explicit selection with no tool selectors allows no tool.
# None (omitted selection) defers to the package default manifest;
# only None at every level means unconstrained.
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from pydantic import BaseModel, ConfigDict, Field

from redteam.core.ids import ToolId

if TYPE_CHECKING:
    pass

class RoleToolSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: str = Field(min_length=1)
    tool_ids: list[str] = Field(default_factory=list)
    server_ids: list[str] = Field(default_factory=list)
    capability_tags: list[str] = Field(default_factory=list)
    methodology_refs: list[str] = Field(default_factory=list)
class RoleManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: str = "builtin"
    version: str = "1"
    roles: list[RoleToolSelection] = Field(default_factory=list)
    def normalized_roles(self) -> list:
        merged: dict = {}
        for role in self.roles:
            entry = merged.setdefault(role.agent_id, {})
            for name in ("tool_ids", "server_ids", "capability_tags", "methodology_refs"):
                entry[name] = sorted(set(entry.get(name, ())) | set(getattr(role, name)))
        ordered = []
        for agent_id in sorted(merged):
            item = {"agent_id": agent_id}
            item.update(merged[agent_id])
            ordered.append(item)
        return ordered
    def fingerprint(self) -> str:
        payload = json.dumps(self.normalized_roles(), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
def default_manifest_path():
    return Path(__file__).resolve().parent / "reference_data" / "roles_default.yaml"
def load_role_manifest(path=None):
    source = Path(path) if path is not None else default_manifest_path()
    raw = source.read_text(encoding="utf-8")
    if source.suffix.lower() in (".yaml", ".yml"):
        data = yaml.safe_load(raw)
    else:
        data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("role manifest must be a mapping")
    return RoleManifest.model_validate(data)
def select_for_role(manifest, agent_id):
    for role in manifest.roles:
        if role.agent_id == agent_id:
            return role
    return None


def _coerce_selection(agent_id, inline):
    """Inline definition selectors into a RoleToolSelection."""
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


def _effective_selection(agent, file_manifest, package_default):
    """Custom file row, else inline, else default row, else capabilities."""
    agent_id = str(getattr(agent, "agent_id", ""))
    if file_manifest is not None:
        row = select_for_role(file_manifest, agent_id)
        if row is not None:
            return row
    inline = getattr(agent, "tool_selection", None)
    if inline is not None:
        return _coerce_selection(agent_id, inline)
    row = select_for_role(package_default, agent_id)
    if row is not None:
        return row
    return _fallback_from_capabilities(agent)


def resolve_role_selection(definition, manifest=None):
    """Read path for the assembled definition: inline first, else manifest.

    After configure_role_tools the inline selection is always set, so
    consumers never re-read a stale default file. A role missing
    everywhere falls back to its own definition capabilities; only a
    role without capabilities stays unconstrained. An explicitly empty
    selection still allows no tool. methodology_refs never filter tools.
    """
    inline = getattr(definition, "tool_selection", None)
    if inline is not None:
        return _coerce_selection(str(getattr(definition, "agent_id", "")), inline)
    if manifest is None:
        manifest = load_role_manifest()
    row = select_for_role(manifest, str(getattr(definition, "agent_id", "")))
    if row is not None:
        return row
    return _fallback_from_capabilities(definition)


def _fallback_from_capabilities(agent):
    """Field-default selection derived from the definition capabilities.

    No central name branching: whatever capabilities the definition
    already declares become the capability tag filter.
    """
    tags = sorted({str(tag) for tag in (getattr(agent, "capabilities", None) or ()) if str(tag)})
    if not tags:
        return None
    return RoleToolSelection(
        agent_id=str(getattr(agent, "agent_id", "")), capability_tags=tags
    )


def role_tool_visible(definition, tool, manifest=None):
    """Same-view predicate shared by context, workers, and execution entry.

    Declared ids compare verbatim here; alias normalization happens
    in tools_for_role and configure_role_tools against a catalog.
    """
    selection = resolve_role_selection(definition, manifest)
    return selection_allows(
        selection,
        tool_id=str(tool.tool_id),
        server_id=str(tool.server_id),
        capability_tags=list(tool.capability_tags),
    )


def tools_for_role(definition, catalog=None, manifest=None):
    """Visible descriptors for one role; operation bindings never filter.

    A None catalog honestly yields no tools. Declared-but-unregistered
    tool ids are an error; declared aliases resolve to registered
    physical ids before matching. Mapper-less tools stay visible
    with an empty operation list from the catalog view.
    """
    if catalog is None:
        return []
    if manifest is None:
        manifest = load_role_manifest()
    selection = resolve_role_selection(definition, manifest)
    if selection is not None and selection.tool_ids:
        selection = selection.model_copy(
            update={"tool_ids": _physical_tool_ids(catalog, selection)}
        )
    visible = []
    for descriptor in catalog.all_descriptors():
        if selection_allows(
            selection,
            tool_id=str(descriptor.tool_id),
            server_id=str(descriptor.server_id),
            capability_tags=list(descriptor.capability_tags),
        ):
            visible.append(descriptor)
    return visible


def _physical_tool_ids(catalog, selection) -> list:
    # Declared ids (aliases included) resolve to registered physical
    # ids through the catalog canonical-first resolver; that existing
    # resolver is the only alias compat relied upon.
    physical = []
    unknown = []
    for tool_id in selection.tool_ids:
        descriptor = catalog.get(ToolId(str(tool_id)))
        if descriptor is None:
            unknown.append(str(tool_id))
        elif str(descriptor.tool_id) not in physical:
            physical.append(str(descriptor.tool_id))
    if unknown:
        raise ValueError(f"role selection references unregistered tools: {unknown}")
    return physical


def configure_role_tools(agents, catalog=None, path=None):
    """Assemble registered roles against one catalog snapshot and inject.

    Precedence is custom file row, else inline definition selectors,
    else the package default file row, else the definition
    capabilities; roles without capabilities stay unconstrained.
    The resolved selection is written back into
    AgentDefinition.tool_selection, so later helpers read the injected
    copy and never re-load a possibly different file. With a None
    catalog explicit tool ids cannot be verified and are a
    configuration error, while other selectors still inject and join
    the fingerprint. Returns the fingerprint of the resolved
    selections (normalized, sorted, deduplicated), so equal contents
    hash equal across different config paths. The app bootstrap owns
    the call; this module owns the semantics.
    """
    agents = list(agents)
    package_default = load_role_manifest()
    file_manifest = load_role_manifest(path) if path is not None else None
    resolved = []
    for agent in agents:
        if isinstance(agent, str):
            raise ValueError("configure_role_tools expects agent definitions")
        selection = _effective_selection(agent, file_manifest, package_default)
        if selection is not None and catalog is not None and selection.tool_ids:
            selection = selection.model_copy(
                update={"tool_ids": _physical_tool_ids(catalog, selection)}
            )
        if selection is not None:
            resolved.append(selection)
            agent.tool_selection = selection.model_dump(mode="json", exclude={"agent_id"})
        if catalog is None:
            if selection is not None and selection.tool_ids:
                raise ValueError(
                    "role selection declares tools with no catalog to verify: "
                    f"{sorted(selection.tool_ids)}"
                )
            continue
        tools_for_role(agent, catalog)
    return RoleManifest(roles=resolved).fingerprint()
def selection_allows(selection, tool_id="", server_id="", capability_tags=()):
    """Whether one tool passes a resolved role selection.

    None means no selection data at any level and stays unconstrained.
    An explicit selection without tool selectors allows nothing.
    """
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
