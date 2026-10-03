"""Opt-in composition: operator policies to Agent metadata, never Agent filesystem IO."""

from __future__ import annotations

from pathlib import Path

from redteam.capability.methodology import load_catalog, safe_path, verify_bundle
from redteam.capability.servers.workspace import WorkspaceIO, WorkspacePolicy
from redteam.core.config import MethodologyConfig
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
    from redteam.core.ids import ToolId

    descriptor = catalog.get(ToolId("analysis.read_file")) if catalog is not None else None
    if (
        descriptor is None
        or "file_read" not in descriptor.operations
        or descriptor.health_status.value == "unhealthy"
        or descriptor.input_schema.get("properties", {}).get("asset_ref", {}).get("const")
        != library.asset_ref
    ):
        raise ValueError("methodology_workspace_read_tool_required")
    # Preserve caller-owned registries and the built-in Analysis verifier seam.
    scoped_agents = AgentRegistry(agents.all(), definitions=agents.definitions())
    scoped_providers = CandidateProviderRegistry()
    for key in providers.provider_ids:
        provider = providers.get(key)
        if provider is None:
            raise ValueError("methodology_provider_registry_inconsistent")
        scoped_providers.register(key, provider)
    spec, definition = reference_agent()
    scoped_agents.register_definition(definition)
    scoped_agents.register(spec)
    scoped_providers.register(REFERENCE_PROVIDER_ID, MethodologyCandidateProvider(library))
    return scoped_agents, scoped_providers
