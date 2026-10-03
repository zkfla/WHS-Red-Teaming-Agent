"""Opt-in composition: operator policies to Agent metadata, never Agent filesystem IO."""

from __future__ import annotations

from pathlib import Path

from redteam.capability.adapters.mcp.naming import legacy_alias_for_tool_id
from redteam.capability.methodology import load_catalog, safe_path, verify_bundle
from redteam.capability.servers.workspace import WorkspaceIO, WorkspacePolicy
from redteam.core.config import MethodologyConfig
from redteam.core.ids import ToolId
from redteam.core.ports.tool_catalog import ToolCatalog
from redteam.orchestration.agents.providers import CandidateProviderRegistry
from redteam.orchestration.agents.registry import AgentRegistry
from redteam.orchestration.methodology import (
    REFERENCE_PROVIDER_ID,
    MethodologyCandidateProvider,
    ReferenceEntry,
    ReferenceLibrary,
    reference_agent,
)


def load_reference_library(config: MethodologyConfig) -> ReferenceLibrary:
    """Trusted composition call; policy files are not read from MCP arguments."""
    if not all(
        Path(p).is_absolute() for p in (config.workspace_policy_path, config.bundle_policy_path)
    ):
        raise ValueError("methodology_absolute_policy_paths_required")
    workspace = WorkspacePolicy.load(config.workspace_policy_path)
    bundle = WorkspacePolicy.load(config.bundle_policy_path)
    prefix = safe_path(config.prefix)
    if (
        prefix == "."
        or Path(workspace.root).resolve() != Path(workspace.root)
        or (Path(workspace.root) / prefix).resolve() != Path(bundle.root)
        or (workspace.asset_ref, workspace.engagement_id, workspace.run_id)
        != (bundle.asset_ref, bundle.engagement_id, bundle.run_id)
    ):
        raise ValueError("methodology_workspace_binding_mismatch")
    WorkspaceIO(workspace)  # owned private root and safe POSIX IO preflight
    if workspace.writable and not any(
        prefix == path or prefix.startswith(path + "/") for path in workspace.readonly_paths
    ):
        raise ValueError("methodology_readonly_prefix_required")
    catalog = load_catalog()
    verify_bundle(bundle, catalog=catalog)
    by_id = {entry.reference_id: entry for entry in catalog.references}
    if len(set(config.reference_ids)) != len(config.reference_ids) or any(
        key not in by_id for key in config.reference_ids
    ):
        raise ValueError("methodology_unknown_or_duplicate_reference")
    sources = {source.source_id: source for source in catalog.sources}
    return ReferenceLibrary(
        asset_ref=workspace.asset_ref,
        engagement_id=workspace.engagement_id,
        run_id=workspace.run_id,
        read_chars=config.read_chars,
        max_context_chars=config.max_context_chars,
        entries=tuple(
            ReferenceEntry(
                reference_id=by_id[key].reference_id,
                path=f"{prefix}/{by_id[key].bundle_path}",
                sha256=by_id[key].sha256,
                title=by_id[key].title,
                domains=by_id[key].domains,
                source_id=by_id[key].source_id,
                source_commit=sources[by_id[key].source_id].commit,
                review_caveat=by_id[key].review_caveat,
            )
            for key in config.reference_ids
        ),
    )


def wire_reference_agent(
    library: ReferenceLibrary,
    catalog: ToolCatalog | None,
    agents: AgentRegistry,
    providers: CandidateProviderRegistry,
) -> tuple[AgentRegistry, CandidateProviderRegistry]:
    descriptor = _reference_reader(library, catalog)
    # Preserve caller-owned registries and the built-in Analysis verifier seam.
    scoped_agents = AgentRegistry(agents.all(), definitions=agents.definitions())
    scoped_providers = CandidateProviderRegistry()
    for key in providers.provider_ids:
        provider = providers.get(key)
        if provider is None:
            raise ValueError("methodology_provider_registry_inconsistent")
        scoped_providers.register(key, provider)
    # Selected contract drives role, candidate, capture, and inspect meanings.
    # Model prompt context for the engine root is left to the root owner.
    spec, definition = reference_agent(library.selected_contract)
    scoped_agents.register_definition(definition)
    scoped_agents.register(spec)
    scoped_providers.register(
        REFERENCE_PROVIDER_ID, MethodologyCandidateProvider(library, descriptor.tool_id)
    )
    return scoped_agents, scoped_providers


def _reference_reader(library: ReferenceLibrary, catalog: ToolCatalog | None):
    contracts = {entry.read_contract.model_dump_json() for entry in library.entries}
    if len(contracts) != 1:
        raise ValueError("methodology_read_contract_mismatch")
    contract = library.entries[0].read_contract
    if contract.asset_binding != "asset_ref_const" or contract.encoding != "utf-8":
        raise ValueError("methodology_read_contract_unsupported")
    candidates = (
        catalog.list_tools_for_action(contract.capability, contract.operation) if catalog else []
    )
    candidates = [
        tool
        for tool in candidates
        if tool.health_status.value != "unhealthy"
        and contract.asset_binding == "asset_ref_const"
        and tool.input_schema.get("properties", {}).get("asset_ref", {}).get("const")
        == library.asset_ref
    ]
    if len(candidates) != 1:
        raise ValueError("methodology_workspace_read_tool_required")
    descriptor = candidates[0]
    return descriptor


def bind_reference_library(
    library: ReferenceLibrary, catalog: ToolCatalog | None
) -> ReferenceLibrary:
    descriptor = _reference_reader(library, catalog)
    proven: list[ToolId] = []
    alias = legacy_alias_for_tool_id(descriptor.tool_id)
    if alias is not None and alias != str(descriptor.tool_id) and catalog is not None:
        try:
            resolved = catalog.get(ToolId(alias))
        except ValueError:
            resolved = None  # Ambiguous old references prove no physical identity.
        if resolved is not None and (resolved.server_id, resolved.tool_name, resolved.tool_id) == (
            descriptor.server_id,
            descriptor.tool_name,
            descriptor.tool_id,
        ):
            proven.append(ToolId(alias))
    return library.model_copy(
        update={"reader_tool_id": descriptor.tool_id, "accepted_reader_ids": tuple(proven)}
    )
