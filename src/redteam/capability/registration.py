"""Provider-owned registration data for MCP profiles and adapters.

The profile branches from settings, the adapter selection plus default
target types plus discovery checks from bootstrap, all live here as data
plus small callables. Central consumers call one generic function each;
they never branch on profile or server names.

Extension wiring (owned by the composition root): every builtin below is
also published as an extension contribution so the root can collect and
bind the same providers through the core extension declarations. Kind
names and contribution-id rules:

- mcp_profile: id is the profile name (vdb_readonly,
  vdb_readonly_extended, page_text_readonly, execution,
  web_search_tavily, workspace_read, workspace_write). Factory takes a
  CapabilityFactoryContext and returns the McpProfileBundle for that
  profile: settings contributor, adapter factory, discovery binder and
  kind-default target types in one provider unit. Every slot is
  optional; partial bundles fall back to the builtin rows. One profile
  never forces the other slots, so requires references stay empty.

Builtin factories ignore the context (frozen data); alternate providers
may read CapabilityFactoryContext.env. Requires references are checked
by the root assembly before any factory runs.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from redteam.core.enums import RiskLevel

if TYPE_CHECKING:
    from redteam.core.extensions import ExtensionDeclaration

CAPABILITY_EXTENSION_ID = "capability-builtin"
CAPABILITY_EXTENSION_VERSION = "1"

KIND_MCP_PROFILE = "mcp_profile"

BUILTIN_PROFILE_NAMES = (
    "vdb_readonly",
    "vdb_readonly_extended",
    "page_text_readonly",
    "execution",
    "web_search_tavily",
    "workspace_read",
    "workspace_write",
)

_OPAQUE_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_RISK_ORDER = (RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL)
_KIND_TARGET_TYPES = {
    "mobile": ["device", "asset"],
    "analysis": ["asset", "endpoint", "observation"],
    "web_api": ["host", "service", "endpoint"],
    "recon": ["host", "service", "endpoint"],
}


@dataclass(frozen=True)
class CapabilityFactoryContext:
    """Context handed to every capability contribution factory."""

    env: Mapping[str, str] = field(default_factory=dict)
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class ProfileSettingsContext:
    """Every knob one profile contributor may read or rewrite."""

    profile: str | None
    prefix: str
    kind: str
    transport: str
    risk_level: RiskLevel
    has_explicit_risk: bool
    allowed_tools: tuple[str, ...] | None
    tool_operations: dict[str, tuple[str, ...]] | None
    args: tuple[str, ...]
    process_environment: dict[str, str]
    env: Mapping[str, str]
    vdb_asset_ref: str | None = None
    workspace_asset_ref: str | None = None
    page_text_config_path: str | None = None
    page_text_config_sha256: str | None = None
    execution_asset_ref: str | None = None
    execution_policy_path: str | None = None
    execution_policy_sha256: str | None = None
    web_search_config_path: str | None = None
    web_search_config_sha256: str | None = None


ProfileContributor = Callable[[ProfileSettingsContext], None]
AdapterContributor = Callable[[Any, Any], Any]
DiscoveryBinder = Callable[[Any, Any], Any]


def _required_env_value(env: Mapping[str, str], name: str) -> str:
    from redteam.capability.settings import MCPConfigurationError

    value = env.get(name)
    if value is None or not value.strip():
        raise MCPConfigurationError(f"required environment variable {name} is not set")
    return value.strip()


def apply_vdb_profile(context: ProfileSettingsContext) -> None:
    """Builtin contributor for vdb_readonly / vdb_readonly_extended."""

    from redteam.capability.settings import MCPConfigurationError
    from redteam.capability.vdb_manifest import vdb_specs

    if context.kind != "analysis" or context.transport != "stdio":
        raise MCPConfigurationError("vdb profile requires analysis kind and stdio")
    asset_ref = _required_env_value(context.env, context.prefix + "_VDB_ASSET_REF")
    if not _OPAQUE_REF.fullmatch(asset_ref):
        raise MCPConfigurationError("vdb asset_ref must be an opaque identifier")
    specs = vdb_specs(cast(str, context.profile))
    if (
        context.allowed_tools is None
        or set(context.allowed_tools) != set(specs)
        or context.tool_operations != {name: (spec.operation,) for name, spec in specs.items()}
    ):
        raise MCPConfigurationError("vdb profile requires exact allowlist and bindings")
    if context.risk_level == RiskLevel.LOW:
        raise MCPConfigurationError("vdb profile requires at least medium risk")
    context.vdb_asset_ref = asset_ref


def apply_page_text_profile(context: ProfileSettingsContext) -> None:
    """Builtin contributor for page_text_readonly."""

    from pathlib import Path

    from redteam.capability.servers.page_text_mcp import (
        MODULE,
        PROPOSED_OPERATION,
        REMOTE_TOOL_NAME,
        load_page_text_config,
    )
    from redteam.capability.settings import MCPConfigurationError

    if context.kind != "web_api" or context.transport != "stdio":
        raise MCPConfigurationError("page text profile requires web_api kind and stdio")
    if not context.has_explicit_risk:
        raise MCPConfigurationError("page text profile requires an explicit risk level")
    config_path = str(
        Path(_required_env_value(context.env, context.prefix + "_PAGE_TEXT_CONFIG")).resolve()
    )
    try:
        _, config_sha256 = load_page_text_config(config_path)
    except ValueError:
        raise MCPConfigurationError("invalid page text operator configuration") from None
    expected_tools: tuple[str, ...] = (REMOTE_TOOL_NAME,)
    expected_bindings: dict[str, tuple[str, ...]] = {REMOTE_TOOL_NAME: (PROPOSED_OPERATION,)}
    if context.allowed_tools is not None and context.allowed_tools != expected_tools:
        raise MCPConfigurationError("page text profile requires its exact tool allowlist")
    if context.tool_operations is not None and context.tool_operations != expected_bindings:
        raise MCPConfigurationError("page text profile requires its exact operation binding")
    if context.args:
        raise MCPConfigurationError("page text profile constructs its own stdio arguments")
    context.allowed_tools = expected_tools
    context.tool_operations = expected_bindings
    context.args = (
        "-m",
        MODULE,
        "--config",
        config_path,
        "--sha256",
        config_sha256,
    )
    context.page_text_config_path = config_path
    context.page_text_config_sha256 = config_sha256


def apply_execution_profile(context: ProfileSettingsContext) -> None:
    """Builtin contributor for the execution profile."""

    from pathlib import Path

    from redteam.capability.execution_manifest import (
        EXECUTION_TOOLS,
        minimum_execution_risk,
    )
    from redteam.capability.servers.execution import (
        MODULE,
        load_execution_policy,
    )
    from redteam.capability.settings import MCPConfigurationError

    if context.kind != "analysis" or context.transport != "stdio":
        raise MCPConfigurationError("execution profile requires analysis kind and stdio")
    if not context.has_explicit_risk:
        raise MCPConfigurationError("execution profile requires an explicit risk level")
    policy_path = str(
        Path(_required_env_value(context.env, context.prefix + "_EXECUTION_POLICY")).resolve()
    )
    try:
        execution_policy, policy_sha256 = load_execution_policy(policy_path)
    except (OSError, ValueError):
        raise MCPConfigurationError("invalid execution operator policy") from None
    context.execution_asset_ref = execution_policy.asset_ref
    expected_tools = tuple(EXECUTION_TOOLS)
    expected_bindings: dict[str, tuple[str, ...]] = {
        name: (spec.operation,) for name, spec in EXECUTION_TOOLS.items()
    }
    if context.allowed_tools is not None and context.allowed_tools != expected_tools:
        raise MCPConfigurationError("execution profile requires its exact tool allowlist")
    if context.tool_operations is not None and context.tool_operations != expected_bindings:
        raise MCPConfigurationError("execution profile requires its exact operation bindings")
    if context.args:
        raise MCPConfigurationError("execution profile constructs its own stdio arguments")
    if _RISK_ORDER.index(context.risk_level) < _RISK_ORDER.index(minimum_execution_risk()):
        raise MCPConfigurationError("execution profile risk cannot be below the minimum")
    context.allowed_tools = expected_tools
    context.tool_operations = expected_bindings
    context.args = (
        "-m",
        MODULE,
        "--policy",
        policy_path,
        "--sha256",
        policy_sha256,
    )
    context.execution_policy_path = policy_path
    context.execution_policy_sha256 = policy_sha256


def apply_web_search_profile(context: ProfileSettingsContext) -> None:
    """Builtin contributor for web_search_tavily."""

    from pathlib import Path

    from redteam.capability.servers.web_search_mcp import (
        MODULE,
        PROPOSED_OPERATION,
        REMOTE_TOOL_NAME,
        load_web_search_config,
    )
    from redteam.capability.settings import MCPConfigurationError

    if context.kind != "web_api" or context.transport != "stdio":
        raise MCPConfigurationError("web search profile requires web_api kind and stdio")
    if not context.has_explicit_risk:
        raise MCPConfigurationError("web search profile requires an explicit risk level")
    if context.process_environment:
        raise MCPConfigurationError("web search profile manages its own secret environment")
    config_path = str(
        Path(_required_env_value(context.env, context.prefix + "_SEARCH_CONFIG")).resolve()
    )
    try:
        search_config, config_sha256 = load_web_search_config(config_path)
    except ValueError:
        raise MCPConfigurationError("invalid web search operator configuration") from None
    key = context.env.get("TAVILY_API_KEY", "")
    if search_config.external_search_enabled and not key:
        raise MCPConfigurationError("TAVILY_API_KEY is required for enabled web search")
    context.process_environment = (
        {"TAVILY_API_KEY": key} if search_config.external_search_enabled else {}
    )
    expected_tools = (REMOTE_TOOL_NAME,)
    expected_bindings: dict[str, tuple[str, ...]] = {REMOTE_TOOL_NAME: (PROPOSED_OPERATION,)}
    if context.allowed_tools is not None and context.allowed_tools != expected_tools:
        raise MCPConfigurationError("web search profile requires its exact tool allowlist")
    if context.tool_operations is not None and context.tool_operations != expected_bindings:
        raise MCPConfigurationError("web search profile requires its exact operation binding")
    if context.args:
        raise MCPConfigurationError("web search profile constructs its own stdio arguments")
    context.allowed_tools = expected_tools
    context.tool_operations = expected_bindings
    context.args = (
        "-m",
        MODULE,
        "--config",
        config_path,
        "--sha256",
        config_sha256,
    )
    context.web_search_config_path = config_path
    context.web_search_config_sha256 = config_sha256


def apply_workspace_profile(context: ProfileSettingsContext) -> None:
    """Builtin contributor for workspace_read / workspace_write."""

    from redteam.capability.settings import MCPConfigurationError
    from redteam.capability.workspace_manifest import (
        minimum_workspace_risk,
        workspace_specs,
    )

    if context.kind != "analysis" or context.transport != "stdio":
        raise MCPConfigurationError("workspace profiles require analysis kind and stdio")
    asset_ref = _required_env_value(context.env, context.prefix + "_WORKSPACE_ASSET_REF")
    if not _OPAQUE_REF.fullmatch(asset_ref):
        raise MCPConfigurationError("workspace asset_ref must be an opaque identifier")
    specs = workspace_specs(cast(str, context.profile))
    if (
        context.allowed_tools is None
        or set(context.allowed_tools) != set(specs)
        or context.tool_operations != {name: (spec.operation,) for name, spec in specs.items()}
    ):
        raise MCPConfigurationError("workspace profile requires exact allowlist and bindings")
    if _RISK_ORDER.index(context.risk_level) < _RISK_ORDER.index(
        minimum_workspace_risk(cast(str, context.profile))
    ):
        raise MCPConfigurationError("workspace profile risk cannot be below the minimum")
    context.workspace_asset_ref = asset_ref


_PROFILE_CONTRIBUTORS: dict[str, ProfileContributor] = {
    "vdb_readonly": apply_vdb_profile,
    "vdb_readonly_extended": apply_vdb_profile,
    "page_text_readonly": apply_page_text_profile,
    "execution": apply_execution_profile,
    "web_search_tavily": apply_web_search_profile,
    "workspace_read": apply_workspace_profile,
    "workspace_write": apply_workspace_profile,
}


def list_builtin_profile_names() -> tuple[str, ...]:
    """Profile names the builtin provider table covers, in a stable order."""

    return BUILTIN_PROFILE_NAMES


def profile_contributor(profile: str) -> ProfileContributor | None:
    """The settings contributor for one profile, or None when unknown."""

    return _PROFILE_CONTRIBUTORS.get(profile)


def apply_profile_settings(
    context: ProfileSettingsContext,
    bundles: Mapping[str, McpProfileBundle] | None = None,
) -> None:
    """Generic consumer: run the contributor for the context profile."""

    from redteam.capability.settings import MCPConfigurationError

    if context.profile is None:
        return
    if bundles is not None:
        bundle = bundles.get(context.profile)
        if bundle is not None:
            if bundle.apply_settings is not None:
                bundle.apply_settings(context)
            return
    contributor = _PROFILE_CONTRIBUTORS.get(context.profile)
    if contributor is None:
        raise MCPConfigurationError(context.prefix + "_PROFILE is not a supported profile")
    contributor(context)


def _build_web_api_adapter(settings: Any, transport: Any) -> Any:
    from redteam.capability.adapters.mcp.web_api import WebAPIMCPAdapter

    return WebAPIMCPAdapter(
        server_id=settings.server_id,
        risk_level=settings.risk_level,
        transport=transport,
    )


def _build_mobile_adapter(settings: Any, transport: Any) -> Any:
    from redteam.capability.adapters.mcp.mobile import MobileMCPAdapter

    return MobileMCPAdapter(
        server_id=settings.server_id,
        risk_level=settings.risk_level,
        transport=transport,
    )


def _build_recon_adapter(settings: Any, transport: Any) -> Any:
    from redteam.capability.adapters.mcp.recon import ReconMCPAdapter

    return ReconMCPAdapter(
        server_id=settings.server_id,
        risk_level=settings.risk_level,
        transport=transport,
    )


def _build_analysis_adapter(settings: Any, transport: Any) -> Any:
    from redteam.capability.adapters.mcp.analysis import AnalysisMCPAdapter

    return AnalysisMCPAdapter(
        server_id=settings.server_id,
        risk_level=settings.risk_level,
        transport=transport,
    )


def _build_execution_adapter(settings: Any, transport: Any) -> Any:
    from redteam.capability.adapters.mcp.execution import ExecutionMCPAdapter

    return ExecutionMCPAdapter(
        server_id=settings.server_id,
        risk_level=settings.risk_level,
        transport=transport,
    )


def _build_vdb_adapter(settings: Any, transport: Any) -> Any:
    from redteam.capability.adapters.mcp.scoped_analysis import VDBMCPAdapter

    return VDBMCPAdapter(
        server_id=settings.server_id,
        risk_level=settings.risk_level,
        transport=transport,
    )


def _build_workspace_adapter(settings: Any, transport: Any) -> Any:
    from redteam.capability.adapters.mcp.workspace import WorkspaceMCPAdapter

    return WorkspaceMCPAdapter(
        server_id=settings.server_id,
        risk_level=settings.risk_level,
        transport=transport,
    )


def _build_mobsf_adapter(settings: Any, transport: Any) -> Any:
    from redteam.capability.adapters.mcp.mobsf import MobSFMCPAdapter

    return MobSFMCPAdapter.from_settings(settings, transport)


_SERVER_ADAPTERS: dict[str, AdapterContributor] = {
    "mobsf": _build_mobsf_adapter,
}

_KIND_ADAPTERS: dict[str, AdapterContributor] = {
    "kind:web_api": _build_web_api_adapter,
    "kind:mobile": _build_mobile_adapter,
    "kind:recon": _build_recon_adapter,
    "kind:analysis": _build_analysis_adapter,
}


def create_adapter_for_settings(
    settings: Any,
    transport: Any,
    bundles: Mapping[str, McpProfileBundle] | None = None,
) -> Any:
    """Generic consumer: server special, then profile bundle, then kind."""

    from redteam.capability.settings import MCPConfigurationError

    server_builder = _SERVER_ADAPTERS.get(str(settings.server_id))
    if server_builder is not None:
        return server_builder(settings, transport)
    if settings.profile is not None:
        table = _PROFILE_BUNDLES if bundles is None else bundles
        bundle = table.get(str(settings.profile))
        if bundle is not None and bundle.build_adapter is not None:
            return bundle.build_adapter(settings, transport)
    kind_builder = _KIND_ADAPTERS.get("kind:" + str(getattr(settings.kind, "value", settings.kind)))
    if kind_builder is not None:
        return kind_builder(settings, transport)
    raise MCPConfigurationError(f"unsupported MCP server kind: {settings.kind}")


def default_target_types_for_kind(kind: Any) -> list[str]:
    """Conservative target hints for discovery metadata."""

    return list(_KIND_TARGET_TYPES.get(getattr(kind, "value", kind), ()))


def project_default_target_binding(descriptor: Any, operations: Collection[str]) -> None:
    """Default descriptor target semantics from bound-operation flags.

    Presence decides, never values: a provider that explicitly
    declares even default-equal flags keeps its declaration.
    Tools bound to zero or several operations keep the generic
    default. No new central list is consulted: the existing
    operation table is reused as the default source.
    """
    from redteam.core.contracts.tool import ToolTargetBinding
    from redteam.core.provider_contracts import operation_target_binding

    bound = [str(operation) for operation in operations]
    if len(bound) != 1:
        return
    if "target_binding" in (getattr(descriptor, "model_fields_set", None) or set()):
        return
    flags = operation_target_binding(bound[0])
    descriptor.target_binding = ToolTargetBinding(
        binds_target_arguments=flags.binds_target_arguments,
        binds_known_path_from_url=flags.binds_known_path_from_url,
        requires_url_target=flags.requires_url_target,
        requires_exact_url_match=flags.requires_exact_url_match,
    )


def _bind_page_text_discovery(descriptor: Any, settings: Any) -> str | None:
    from redteam.capability.page_text_manifest import validate_page_text_discovery
    from redteam.capability.servers.page_text_mcp import load_page_text_config
    from redteam.capability.settings import MCPConfigurationError

    descriptor.target_types = ["url"]
    descriptor.outbound_text_fields = ["url"]
    try:
        config, _ = load_page_text_config(
            settings.page_text_config_path or "",
            settings.page_text_config_sha256,
        )
        validate_page_text_discovery(descriptor, config)
    except ValueError:
        raise MCPConfigurationError(
            "page text discovery differs from operator configuration"
        ) from None
    return "url"


def _bind_execution_discovery(descriptor: Any, settings: Any) -> str | None:
    from redteam.capability.execution_manifest import validate_execution_descriptor
    from redteam.capability.settings import MCPConfigurationError

    descriptor.target_types = ["asset"]
    try:
        validate_execution_descriptor(
            descriptor,
            settings.execution_asset_ref or "",
        )
    except ValueError as exc:
        raise MCPConfigurationError(str(exc)) from exc
    return "asset"


def _bind_web_search_discovery(descriptor: Any, settings: Any) -> str | None:
    from redteam.capability.servers.web_search_mcp import load_web_search_config
    from redteam.capability.settings import MCPConfigurationError
    from redteam.capability.web_search_manifest import validate_web_search_discovery

    descriptor.target_types = []
    descriptor.requires_target = False
    descriptor.outbound_text_fields = ["query"]
    try:
        search_config, _ = load_web_search_config(
            settings.web_search_config_path or "",
            settings.web_search_config_sha256,
        )
        validate_web_search_discovery(descriptor, search_config)
    except ValueError:
        raise MCPConfigurationError(
            "web search discovery differs from operator configuration"
        ) from None
    return None


def _bind_vdb_discovery(descriptor: Any, settings: Any) -> str | None:
    from redteam.capability.settings import MCPConfigurationError
    from redteam.capability.vdb_manifest import validate_vdb_descriptor

    descriptor.target_types = ["asset"]
    try:
        validate_vdb_descriptor(descriptor, settings.vdb_asset_ref or "", settings.profile)
    except ValueError as exc:
        raise MCPConfigurationError(str(exc)) from exc
    return "asset"


def _bind_workspace_discovery(descriptor: Any, settings: Any) -> str | None:
    from redteam.capability.settings import MCPConfigurationError
    from redteam.capability.workspace_manifest import validate_workspace_descriptor

    descriptor.target_types = ["asset"]
    try:
        validate_workspace_descriptor(
            descriptor,
            settings.profile,
            settings.workspace_asset_ref or "",
        )
    except ValueError as exc:
        raise MCPConfigurationError(str(exc)) from exc
    return "asset"


_PROFILE_DISCOVERY_BINDERS: dict[str, DiscoveryBinder] = {
    "page_text_readonly": _bind_page_text_discovery,
    "execution": _bind_execution_discovery,
    "web_search_tavily": _bind_web_search_discovery,
    "vdb_readonly": _bind_vdb_discovery,
    "vdb_readonly_extended": _bind_vdb_discovery,
    "workspace_read": _bind_workspace_discovery,
    "workspace_write": _bind_workspace_discovery,
}


@dataclass(frozen=True)
class McpProfileBundle:
    profile: str
    apply_settings: ProfileContributor | None = None
    build_adapter: AdapterContributor | None = None
    bind_discovery: DiscoveryBinder | None = None
    default_target_types: tuple[str, ...] = ()


_PROFILE_BUNDLES: dict[str, McpProfileBundle] = {
    "vdb_readonly": McpProfileBundle(
        profile="vdb_readonly",
        apply_settings=apply_vdb_profile,
        build_adapter=_build_vdb_adapter,
        bind_discovery=_bind_vdb_discovery,
        default_target_types=tuple(_KIND_TARGET_TYPES.get("analysis", ())),
    ),
    "vdb_readonly_extended": McpProfileBundle(
        profile="vdb_readonly_extended",
        apply_settings=apply_vdb_profile,
        build_adapter=_build_vdb_adapter,
        bind_discovery=_bind_vdb_discovery,
        default_target_types=tuple(_KIND_TARGET_TYPES.get("analysis", ())),
    ),
    "page_text_readonly": McpProfileBundle(
        profile="page_text_readonly",
        apply_settings=apply_page_text_profile,
        build_adapter=_build_web_api_adapter,
        bind_discovery=_bind_page_text_discovery,
        default_target_types=tuple(_KIND_TARGET_TYPES.get("web_api", ())),
    ),
    "execution": McpProfileBundle(
        profile="execution",
        apply_settings=apply_execution_profile,
        build_adapter=_build_execution_adapter,
        bind_discovery=_bind_execution_discovery,
        default_target_types=tuple(_KIND_TARGET_TYPES.get("analysis", ())),
    ),
    "web_search_tavily": McpProfileBundle(
        profile="web_search_tavily",
        apply_settings=apply_web_search_profile,
        build_adapter=_build_web_api_adapter,
        bind_discovery=_bind_web_search_discovery,
        default_target_types=tuple(_KIND_TARGET_TYPES.get("web_api", ())),
    ),
    "workspace_read": McpProfileBundle(
        profile="workspace_read",
        apply_settings=apply_workspace_profile,
        build_adapter=_build_workspace_adapter,
        bind_discovery=_bind_workspace_discovery,
        default_target_types=tuple(_KIND_TARGET_TYPES.get("analysis", ())),
    ),
    "workspace_write": McpProfileBundle(
        profile="workspace_write",
        apply_settings=apply_workspace_profile,
        build_adapter=_build_workspace_adapter,
        bind_discovery=_bind_workspace_discovery,
        default_target_types=tuple(_KIND_TARGET_TYPES.get("analysis", ())),
    ),
}


def profile_bundle(profile: str) -> McpProfileBundle | None:
    return _PROFILE_BUNDLES.get(profile)


def builtin_profile_bundles() -> dict[str, McpProfileBundle]:
    return dict(_PROFILE_BUNDLES)


def register_profile_bundle(bundle: McpProfileBundle, *, replace: bool = False) -> None:
    if bundle.profile in _PROFILE_BUNDLES and not replace:
        raise ValueError("profile bundle is already registered: " + bundle.profile)
    _PROFILE_BUNDLES[bundle.profile] = bundle


def apply_profile_discovery_binding(
    descriptor: Any,
    settings: Any,
    bundles: Mapping[str, McpProfileBundle] | None = None,
) -> str | None:
    """Generic consumer: validate one descriptor against its profile."""

    if bundles is not None:
        bundle = bundles.get(settings.profile)
        if bundle is not None:
            if bundle.bind_discovery is not None:
                return bundle.bind_discovery(descriptor, settings)
            return None
    binder = _PROFILE_DISCOVERY_BINDERS.get(settings.profile)
    if binder is None:
        from redteam.capability.settings import MCPConfigurationError

        raise MCPConfigurationError(f"unsupported MCP server profile: {settings.profile}")
    return binder(descriptor, settings)


def select_server_target_type(server_id: str, tool_id: str) -> str | None:
    """Pinned per-tool target hint for conformance-checked servers."""

    from redteam.capability.manifest import split_tool_id
    from redteam.capability.mobile_analysis_manifest import MOBILE_ANALYSIS_TOOLS

    specs = MOBILE_ANALYSIS_TOOLS.get(server_id, ())
    if not specs:
        return None
    split = split_tool_id(tool_id)
    if split is None:
        return None
    _, tool_server, tool_remote = split
    if tool_server is not None and tool_server != server_id:
        return None
    for spec in specs:
        if tool_remote == spec.tool_id.partition(".")[2]:
            return spec.target_type
    return None


def validate_server_configured_discovery(
    server_id: str,
    descriptors: Any,
    server_bindings: Mapping[str, Any],
    server_schemas: Mapping[str, Any],
) -> None:
    """Exact-match check of pinned servers; a no-op for other servers."""

    from redteam.capability.mobile_analysis_manifest import (
        MOBILE_ANALYSIS_TOOLS,
        validate_configured_discovery,
    )
    from redteam.capability.settings import MCPConfigurationError

    if server_id not in MOBILE_ANALYSIS_TOOLS:
        return
    bindings: dict[str, tuple[str, ...]] = {
        str(key): tuple(value) for key, value in server_bindings.items()
    }
    schemas: dict[str, dict[str, Any] | None] = {
        str(key): value for key, value in server_schemas.items()
    }
    try:
        validate_configured_discovery(server_id, descriptors, bindings, schemas)
    except ValueError as exc:
        raise MCPConfigurationError(
            f"MCP server {server_id!r} discovery differs from configured manifest: {exc}"
        ) from exc


def apply_server_catalog_target_types(
    server_id: str,
    server_bindings: Mapping[str, Any],
    target_types: dict[str, Any],
) -> None:
    """Copy pinned per-tool target hints into the gateway catalog map."""

    from redteam.capability.manifest import split_tool_id
    from redteam.capability.mobile_analysis_manifest import MOBILE_ANALYSIS_TOOLS

    specs = MOBILE_ANALYSIS_TOOLS.get(server_id, ())
    if not specs:
        return
    by_remote = {spec.tool_id.partition(".")[2]: spec for spec in specs}
    for binding_key in server_bindings:
        split = split_tool_id(binding_key)
        if split is None:
            continue
        _, tool_server, tool_remote = split
        if tool_server is not None and tool_server != server_id:
            continue
        spec = by_remote.get(tool_remote)
        if spec is not None:
            target_types[binding_key] = spec.target_type


def _mcp_contributions() -> list:
    """Builtin mcp_profile bundle contributions, one per profile."""

    from redteam.core.extensions import Contribution

    def _profile_factory(name: str) -> Callable[[Any], McpProfileBundle | None]:
        def provide(_context: Any) -> McpProfileBundle | None:
            return profile_bundle(name)

        return provide

    return [
        Contribution(
            kind=KIND_MCP_PROFILE,
            contribution_id=name,
            version="1",
            factory=_profile_factory(name),
        )
        for name in BUILTIN_PROFILE_NAMES
    ]


def builtin_mcp_registration_declaration() -> ExtensionDeclaration:
    """Builtin profile/adapter/discovery providers as one declaration."""

    from redteam.core.extensions import ExtensionDeclaration

    return ExtensionDeclaration(
        extension_id=CAPABILITY_EXTENSION_ID,
        version=CAPABILITY_EXTENSION_VERSION,
        contributions=tuple(_mcp_contributions()),
    )


def builtin_capability_declaration() -> ExtensionDeclaration:
    """All builtin capability providers as a single declaration."""

    from redteam.capability.action_binding import (
        extension_contributions as action_contributions,
    )
    from redteam.capability.execution_recipes import (
        extension_contributions as recipe_contributions,
    )
    from redteam.core.extensions import ExtensionDeclaration

    return ExtensionDeclaration(
        extension_id=CAPABILITY_EXTENSION_ID,
        version=CAPABILITY_EXTENSION_VERSION,
        contributions=(
            *_mcp_contributions(),
            *action_contributions(),
            *recipe_contributions(),
        ),
    )
