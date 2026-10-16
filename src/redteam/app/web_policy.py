"""Confirmed web scope -> immutable local MCP deployment, before any model/tool.

Uses the capability-owned schema, generator and role metadata. Generated MCP
settings are session inputs; neither process environment nor .env is modified.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from redteam.app.mcp_deployment import DeploymentBinding, synchronize_deployment
from redteam.capability.deployment import (
    DeploymentSpec,
    SessionAccess,
    check_origin_scope,
    connection_environment,
    deployment_metadata,
    digest,
    encoded,
    provision,
    validate_session_access,
)
from redteam.capability.direct_target import TargetNetworkPreparation
from redteam.capability.server_roles import role_spec
from redteam.capability.servers.config import (
    HTTPMethod,
    ServerPolicy,
    SessionProfile,
    TargetProfile,
    read_methods,
)
from redteam.capability.settings import MCPServerSettings, mcp_server_ids_from_environment
from redteam.core.config import EngagementConfig, load_engagement_config
from redteam.core.environment import env_snapshot

WEB_POLICY_KEY = "web_policy"
KALI_NETWORK_MODE_KEY = "kali_network_mode"
DEPLOYMENT_KEYS = frozenset({"mcp_deployment", "mcp_targets", "target_refs", "auth_sessions"})


class WebSessionSelection(BaseModel):
    """Credential references only; never passwords/tokens supplied to a model."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    session_ref: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,80}$")
    principal_ref: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,80}$")
    origin: str
    paths: list[str] = Field(min_length=1, max_length=100)
    bearer_env: str | None = None
    storage_state_path: str | None = None
    expected_access: Literal["allow", "deny", "unspecified"] = "unspecified"


_PERMISSION_DEFAULTS = {
    "permissions": "read_only", "methods": ["GET", "HEAD", "OPTIONS"],
    "query_keys": [], "allow_all_query_keys": False, "request_headers": [],
    "allow_all_request_headers": False, "request_body_enabled": False,
    "response_body_max_chars": 0, "sessions": [], "kali_network_mode": "inherit",
    "kali_network_policy_path": None, "kali_network_policy_sha256": None,
    "kali_network_preparation": None,
    "max_concurrency": 1,
}


class WebPolicySelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    mode: Literal["site", "paths"]
    origins: list[str] = Field(min_length=1, max_length=30)
    paths: list[str] = Field(default_factory=list, max_length=100)
    permissions: Literal["read_only", "custom", "full"] = "read_only"
    methods: list[HTTPMethod] = Field(default_factory=read_methods)
    query_keys: list[str] = Field(default_factory=list, max_length=100)
    allow_all_query_keys: bool = False
    request_headers: list[str] = Field(default_factory=list, max_length=100)
    allow_all_request_headers: bool = False
    request_body_enabled: bool = False
    response_body_max_chars: int = Field(default=0, ge=0, le=16384)
    max_concurrency: int = Field(default=1, ge=1, le=32)
    sessions: list[WebSessionSelection] = Field(default_factory=list, max_length=60)
    kali_network_mode: Literal["inherit", "closed", "configured", "prepare"] = "inherit"
    kali_network_policy_path: str | None = None
    kali_network_policy_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    kali_network_preparation: TargetNetworkPreparation | None = None

    @model_validator(mode="after")
    def explicit_subjects(self):
        hosts = set()
        for origin in self.origins:
            TargetProfile(origin=origin, allowed_paths=["/"])
            host = urlsplit(origin).hostname
            if host in hosts:
                raise ValueError("one_origin_per_host_required")
            hosts.add(host)
        if self.mode == "paths" and not self.paths:
            raise ValueError("paths_required")
        if self.mode == "site" and self.paths:
            raise ValueError("site_mode_has_no_path_list")
        TargetProfile.valid_paths(self.paths)
        if any(any(char in path for char in "*{}") for path in self.paths):
            raise ValueError("literal_paths_required")
        if self.permissions == "read_only" and (
            self.methods != ["GET", "HEAD", "OPTIONS"] or self.query_keys
            or self.allow_all_query_keys or self.request_headers or self.allow_all_request_headers
            or self.request_body_enabled
        ):
            raise ValueError("read_only_permissions_have_no_request_overrides")
        if self.kali_network_mode == "configured":
            if not self.kali_network_policy_path or not self.kali_network_policy_sha256:
                raise ValueError("kali_network_policy_required")
            if not Path(self.kali_network_policy_path).is_absolute():
                raise ValueError("kali_network_policy_absolute_path_required")
        elif self.kali_network_policy_path or self.kali_network_policy_sha256:
            raise ValueError("kali_network_policy_not_selected")
        if (self.kali_network_mode == "prepare") != (self.kali_network_preparation is not None):
            raise ValueError("kali_network_preparation_selection_mismatch")
        if len({session.session_ref for session in self.sessions}) != len(self.sessions):
            raise ValueError("duplicate_session_ref")
        if any(session.origin not in self.origins for session in self.sessions):
            raise ValueError("session_origin_not_selected")
        # Declaration validation needs neither a fabricated network nor a DNS/tool call.
        for origin in self.origins:
            self.target_profile(origin)
        bindings = {urlsplit(origin).hostname: self.target_ref(origin) for origin in self.origins}
        sessions, access = self.session_bindings(bindings)
        for selection in self.sessions:
            ref = selection.session_ref
            validate_session_access(ref, sessions[ref], access[ref],
                                    self.target_profile(selection.origin))
        return self

    @model_serializer(mode="wrap")
    def compatible_payload(self, handler):
        value = handler(self)
        for key, default in _PERMISSION_DEFAULTS.items():
            if value.get(key) == default:
                value.pop(key, None)
        return value

    def target_profile(self, origin: str) -> TargetProfile:
        full = self.permissions == "full"
        methods: list[HTTPMethod] = (
            ["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"] if full else self.methods
        )
        return TargetProfile(
            origin=origin, allowed_paths=self.paths, allow_all_paths=self.mode == "site",
            response_body_max_chars=self.response_body_max_chars,
            allowed_methods=methods,
            allow_all_query_keys=full or self.allow_all_query_keys,
            allowed_global_query_keys=[] if full else self.query_keys,
            allow_all_request_headers=full or self.allow_all_request_headers,
            allowed_request_headers=[] if full else self.request_headers,
            request_body_enabled=(True if self.permissions == "read_only"
                                  else full or self.request_body_enabled),
        )

    def spec(self, networks: list[str]) -> DeploymentSpec:
        targets, bindings = {}, {}
        for origin in self.origins:
            host = urlsplit(origin).hostname
            assert host is not None
            ref = self.target_ref(origin)
            targets[ref] = self.target_profile(origin)
            bindings[host] = ref
        sessions, access = self.session_bindings(bindings)
        return DeploymentSpec(
            request_permissions="read_only" if self.permissions == "read_only" else "testing",
            policy=ServerPolicy(
                targets=targets, allowed_networks=networks, sessions=sessions,
                max_concurrency=self.max_concurrency,
            ),
            host_bindings=bindings, session_access=access,
        )

    @staticmethod
    def target_ref(origin: str) -> str:
        return "target_" + hashlib.sha256(origin.encode()).hexdigest()[:16]

    def session_bindings(self, bindings):
        sessions, access = {}, {}
        for session in self.sessions:
            host = urlsplit(session.origin).hostname
            assert host is not None
            sessions[session.session_ref] = SessionProfile(
                target_ref=bindings[host], principal_ref=session.principal_ref,
                bearer_env=session.bearer_env, storage_state_path=session.storage_state_path,
                allowed_paths=session.paths,
            )
            access[session.session_ref] = SessionAccess(
                paths=session.paths, expected_access=session.expected_access,
            )
        return sessions, access


def validate_kali_network_policy(path: str, origins: list[str], expected_sha256=None):
    """Use the existing gate, with no additional destinations or package mirrors."""
    from redteam.capability.shell_assembly import build_gated_provider_config
    if not Path(path).is_absolute():
        raise ValueError("kali_network_policy_absolute_path_required")
    content = Path(path).read_bytes()
    fingerprint = digest(content)
    if expected_sha256 and fingerprint != expected_sha256:
        raise ValueError("kali_network_policy_changed")
    provider = build_gated_provider_config(path, policy_bytes=content)
    assert provider is not None
    hosts = {urlsplit(origin).hostname for origin in origins}
    if not provider.network_policy.registry.targets or (
        set(provider.network_policy.registry.targets) - hosts
        or provider.network_policy.registry.mirrors or provider.network_policy.install_proxy
    ):
        raise ValueError("kali_network_policy_outside_selected_targets")
    return fingerprint, provider


def permissions_expand(before: WebPolicySelection, after: WebPolicySelection) -> bool:
    """Compare effective permissions, not the UI preset name or unused form values."""
    left = before.target_profile(before.origins[0])
    right = after.target_profile(after.origins[0])
    if set(right.allowed_methods) - set(left.allowed_methods):
        return True
    if not left.allow_all_query_keys and (
        right.allow_all_query_keys
        or set(right.allowed_global_query_keys) - set(left.allowed_global_query_keys)
    ):
        return True
    if not left.allow_all_request_headers and (
        right.allow_all_request_headers
        or set(right.allowed_request_headers) - set(left.allowed_request_headers)
    ):
        return True
    if (right.request_body_enabled and not left.request_body_enabled
            or right.response_body_max_chars > left.response_body_max_chars):
        return True
    original_sessions = {session.session_ref: session for session in before.sessions}
    for session in after.sessions:
        original = original_sessions.get(session.session_ref)
        if original is None or set(session.paths) - set(original.paths) or any(
            getattr(session, key) != getattr(original, key) for key in (
                "origin", "principal_ref", "bearer_env", "storage_state_path",
            )
        ):
            return True
    return after.kali_network_mode != "closed" and any(
        getattr(before, key) != getattr(after, key) for key in (
            "kali_network_mode", "kali_network_policy_path", "kali_network_policy_sha256",
            "kali_network_preparation",
        )
    )


async def selected_target_networks(selection: WebPolicySelection,
                                   config: EngagementConfig) -> list[str]:
    """Pin explicitly selected hosts, never default to an unrestricted CIDR."""
    if config.scope.allowed_networks:
        return list(config.scope.allowed_networks)
    networks = set()
    for origin in selection.origins:
        parsed = urlsplit(origin)
        host = parsed.hostname
        assert host is not None
        try:
            addresses = [ipaddress.ip_address(host)]
        except ValueError:
            try:
                entries = await asyncio.wait_for(
                    asyncio.get_running_loop().getaddrinfo(
                        host, parsed.port or (443 if parsed.scheme == "https" else 80),
                        type=socket.SOCK_STREAM,
                    ), timeout=5,
                )
                addresses = [ipaddress.ip_address(entry[4][0]) for entry in entries]
            except (OSError, ValueError, asyncio.TimeoutError):
                raise ValueError("web_policy_dns_resolution_failed") from None
        for address in addresses:
            if (address.is_link_local or address.is_multicast or address.is_unspecified
                    or getattr(address, "ipv4_mapped", None)):
                raise ValueError("web_policy_prohibited_target_address")
            networks.add(f"{address}/{address.max_prefixlen}")
    if not networks:
        raise ValueError("web_policy_no_target_addresses")
    return sorted(networks)


def deployment_environment(spec: DeploymentSpec, binding: DeploymentBinding, base=None) -> dict:
    """Replace selected built-in web providers; preserve unrelated MCP selections."""
    environment = env_snapshot(base)
    kinds = set()
    for role in spec.roles:
        definition = role_spec(role)
        if definition is None:
            raise ValueError("web_policy_role_not_registered")
        kinds.add(definition.kind)
    keep = []
    for identity in mcp_server_ids_from_environment(source=environment):
        if identity in spec.roles:
            continue
        settings = MCPServerSettings.from_environment(identity, env=environment)
        if settings.kind.value not in kinds:
            keep.append(identity)
    environment.update(connection_environment(spec, binding.policy_path, binding.policy_sha256))
    environment["REDTEAM_MCP_SERVERS"] = ",".join([*keep, *spec.roles])
    return environment


async def prepare_web_deployment(path: Path) -> dict[str, str]:
    """Generate once, then validate/reuse the exact source and policy on restart."""
    from redteam.app.web.engagement import EngagementStore

    config = load_engagement_config(path)
    raw = config.metadata.get(WEB_POLICY_KEY)
    if raw is None:
        environment = env_snapshot()
        if config.metadata.get(KALI_NETWORK_MODE_KEY) == "closed":
            environment["REDTEAM_GATED_NETWORK_POLICY_PATH"] = ""
        return environment  # Explicit manual/remote configuration stays selected.
    selection = WebPolicySelection.model_validate(raw)
    root = path.resolve().parent / (path.stem + ".mcp")
    if config.metadata.get("mcp_deployment") is not None:
        binding = DeploymentBinding.model_validate(config.metadata["mcp_deployment"])
        # Only artifacts in this engagement's generated directory are reused.
        for artifact in (binding.source_path, binding.policy_path):
            if not Path(artifact).resolve().is_relative_to(root.resolve()):
                raise ValueError("web_policy_artifact_location_mismatch")
        spec = DeploymentSpec.model_validate_json(Path(binding.source_path).read_bytes())
        wanted = selection.spec(list(spec.policy.allowed_networks))
        if encoded(wanted.model_dump(mode="json")) != encoded(spec.model_dump(mode="json")):
            raise ValueError("web_policy_selection_changed")
        synchronized = synchronize_deployment(config, list(spec.roles), check_environment=False)
        if synchronized != config:
            raise ValueError("web_policy_declarations_changed")
    else:
        # Scope is proved before DNS resolution; no request to a target is made.
        for origin in selection.origins:
            check_origin_scope(origin, config.scope)
        networks = await selected_target_networks(selection, config)
        spec = selection.spec(networks)
        fingerprint = digest(encoded({"spec": spec.model_dump(mode="json"),
                                      "scope": config.scope.model_dump(mode="json")}))
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        output = root / fingerprint
        if output.exists():
            generated = load_engagement_config(output / "engagement.yaml")
            binding = DeploymentBinding.model_validate(generated.metadata["mcp_deployment"])
            probe = config.model_copy(update={"metadata": {
                **config.metadata, **deployment_metadata(spec, config.scope),
                "mcp_deployment": binding.model_dump(),
            }})
            synchronize_deployment(probe, list(spec.roles), check_environment=False)
        else:
            generated = load_engagement_config(provision(spec, config, output))
            binding = DeploymentBinding.model_validate(generated.metadata["mcp_deployment"])
        EngagementStore(path).install_deployment(config, generated.metadata)
    environment = deployment_environment(spec, binding)
    if selection.kali_network_mode in {"closed", "prepare"}:
        environment["REDTEAM_GATED_NETWORK_POLICY_PATH"] = ""
    elif selection.kali_network_mode == "configured":
        assert selection.kali_network_policy_path is not None
        fingerprint, provider = validate_kali_network_policy(
            selection.kali_network_policy_path, selection.origins,
            expected_sha256=selection.kali_network_policy_sha256,
        )
        from redteam.capability.servers.network import resolve_target
        # Pin each registry name to that exact selected host, never merely a broad scope CIDR.
        for host, ips in provider.network_policy.registry.targets.items():
            target = await resolve_target(spec.policy, spec.host_bindings[host])
            if set(ips) - set(target.addresses):
                raise ValueError("kali_network_policy_address_mismatch")
        destination = Path(binding.policy_path).parent / ("kali-network-" + fingerprint + ".json")
        content = Path(selection.kali_network_policy_path).read_bytes()
        if digest(content) != fingerprint:
            raise ValueError("kali_network_policy_changed")
        if destination.exists():
            if digest(destination.read_bytes()) != fingerprint:
                raise ValueError("kali_network_policy_snapshot_changed")
        else:
            import os
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
        environment["REDTEAM_GATED_NETWORK_POLICY_PATH"] = str(destination)
    return environment


@dataclass(frozen=True)
class EngagementStartupSelection:
    """Request-local startup environment and the engagement it was resolved for."""

    environment: dict[str, str]
    engagement: EngagementConfig
    network_policy_path: str | None = None


async def select_engagement_startup(
    engagement: EngagementConfig | Path | str,
    *,
    engagement_path: Path | str | None = None,
    require_recorded_preparation: bool = False,
) -> EngagementStartupSelection:
    """Apply the selected engagement's web startup once, before MCP/Kali assembly.

    Delegates to prepare_web_deployment so the direct-target baseline stays the single
    source of startup environment; this adapter only supplies the selection contract
    that run, resume, and session callers consume.
    """
    if isinstance(engagement, (str, Path)):
        path: Path | None = Path(engagement)
        config = load_engagement_config(engagement)
    else:
        config = engagement
        path = Path(engagement_path) if engagement_path is not None else None
    if config.metadata.get(WEB_POLICY_KEY) is None:
        environment = env_snapshot()
    elif path is None:
        raise ValueError("web_policy_artifact_location_required")
    else:
        if require_recorded_preparation and config.metadata.get("mcp_deployment") is None:
            raise ValueError("kali_preparation_record_missing")
        environment = await prepare_web_deployment(path)
        config = load_engagement_config(path)
    network_policy_path = environment.get("REDTEAM_GATED_NETWORK_POLICY_PATH") or None
    return EngagementStartupSelection(environment, config, network_policy_path)
