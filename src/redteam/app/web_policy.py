"""Confirmed web scope -> immutable local MCP deployment, before any model/tool.

Uses the capability-owned schema, generator and role metadata. Generated MCP
settings are session inputs; neither process environment nor .env is modified.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import socket
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

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
from redteam.capability.gated_network_policy import (
    MAX_ENDPOINTS_PER_TARGET,
    SUPPORTED_GATED_PRESETS,
    GatedLimits,
    GatedNetworkPolicy,
    GatedNetworkPolicyRequest,
    ManagedSidecarRequest,
    SelectedEndpoint,
    build_gated_network_policy,
    policy_content_hash,
)
from redteam.capability.server_roles import role_spec
from redteam.capability.servers.config import (
    HTTPMethod,
    ServerPolicy,
    SessionProfile,
    TargetProfile,
    read_methods,
)
from redteam.capability.settings import MCPServerSettings, mcp_server_ids_from_environment
from redteam.capability.shell_policy import load_shell_workroom_policy, validate_pinned_image
from redteam.core.config import EngagementConfig, load_engagement_config
from redteam.core.contracts.target import EndpointTarget
from redteam.core.environment import env_snapshot
from redteam.core.scope_rules import check_target_scope

WEB_POLICY_KEY = "web_policy"
GATED_PREPARATION_KEY = "gated_network_preparation"
DEPLOYMENT_KEYS = frozenset({
    "mcp_deployment", "mcp_targets", "target_refs", "auth_sessions",
    GATED_PREPARATION_KEY,
})


class WebEndpointSelection(SelectedEndpoint):
    """A selected S endpoint tied to one already-approved web origin."""

    origin: str

    @field_validator("origin")
    @classmethod
    def _selected_origin(cls, value: str) -> str:
        try:
            parts = urlsplit(value)
            if (parts.scheme not in {"http", "https"} or not parts.hostname
                    or parts.username is not None or parts.password is not None
                    or parts.path not in {"", "/"} or parts.query or parts.fragment):
                raise ValueError
            # Parse the port now so malformed values never reach startup.
            _ = parts.port
        except ValueError:
            raise ValueError("kali_endpoint_origin_invalid") from None
        return value


_PREPARE_SELECTION_DEFAULTS: dict[str, object] = {
    "kali_endpoints": [],
    "kali_limits_preset": None,
    "kali_limits": None,
    "kali_sidecar_mode": None,
    "kali_sidecar_image": None,
    "kali_docker_context": None,
}


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
    sessions: list[WebSessionSelection] = Field(default_factory=list, max_length=60)
    kali_network_mode: Literal["inherit", "closed", "configured", "prepare"] = "inherit"
    kali_network_policy_path: str | None = None
    kali_network_policy_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    kali_endpoints: list[WebEndpointSelection] = Field(default_factory=list, max_length=960)
    kali_limits_preset: Literal["probe", "standard", "custom"] | None = None
    kali_limits: GatedLimits | None = None
    kali_sidecar_mode: Literal["managed", "pinned"] | None = None
    kali_sidecar_image: str | None = Field(default=None, min_length=1, max_length=256)
    kali_docker_context: str | None = Field(default=None, min_length=1, max_length=256)

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
        if self.kali_network_mode == "prepare":
            hosts = {urlsplit(origin).hostname for origin in self.origins}
            endpoint_hosts = {urlsplit(item.origin).hostname for item in self.kali_endpoints}
            if not self.kali_endpoints or endpoint_hosts != hosts:
                raise ValueError("kali_endpoint_selection_incomplete")
            grouped: dict[str, list[WebEndpointSelection]] = {}
            seen: set[tuple[str, int, str]] = set()
            for endpoint in self.kali_endpoints:
                if endpoint.origin not in self.origins:
                    raise ValueError("kali_endpoint_origin_not_selected")
                host = (urlsplit(endpoint.origin).hostname or "").lower()
                identity = (host, endpoint.port, endpoint.transport)
                if identity in seen:
                    raise ValueError("kali_endpoint_duplicate")
                seen.add(identity)
                grouped.setdefault(host, []).append(endpoint)
            if any(len(items) > MAX_ENDPOINTS_PER_TARGET for items in grouped.values()):
                raise ValueError("kali_endpoint_too_many")
            if self.kali_limits_preset in {"probe", "standard"} and self.kali_limits is not None:
                raise ValueError("kali_limits_selection_conflict")
            if self.kali_limits_preset == "custom" and self.kali_limits is None:
                raise ValueError("kali_custom_limits_required")
            if self.kali_limits_preset is None:
                raise ValueError("kali_limits_selection_required")
            if self.kali_sidecar_mode is None:
                raise ValueError("kali_sidecar_selection_required")
            if self.kali_sidecar_mode == "pinned":
                if not self.kali_sidecar_image:
                    raise ValueError("kali_sidecar_image_required")
                try:
                    validate_pinned_image(self.kali_sidecar_image)
                except ValueError:
                    raise ValueError("kali_sidecar_image_unpinned") from None
            elif self.kali_sidecar_image is not None:
                raise ValueError("kali_sidecar_image_not_selected")
            if self.kali_docker_context is not None and any(
                char.isspace() or char == "\x00" for char in self.kali_docker_context
            ):
                raise ValueError("kali_docker_context_invalid")
        elif any(
            getattr(self, name) != default
            for name, default in _PREPARE_SELECTION_DEFAULTS.items()
        ):
            raise ValueError("kali_prepare_fields_not_selected")
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
        for key, default in _PREPARE_SELECTION_DEFAULTS.items():
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
            policy=ServerPolicy(targets=targets, allowed_networks=networks, sessions=sessions),
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
    network_expansion = after.kali_network_mode != "closed" and any(
        getattr(before, key) != getattr(after, key) for key in (
            "kali_network_mode", "kali_network_policy_path", "kali_network_policy_sha256",
        )
    )
    if network_expansion:
        return True
    if after.kali_network_mode == "prepare":
        if before.kali_network_mode != "prepare" or before.kali_endpoints != after.kali_endpoints:
            return True
        before_limits = _effective_limits(before)
        after_limits = _effective_limits(after)
        if before_limits is None or after_limits is None:
            return before_limits != after_limits
        return any(
            getattr(after_limits, field) is not None
            and (getattr(before_limits, field) is None
                 or getattr(after_limits, field) > getattr(before_limits, field))
            for field in (
                "max_flow_init_deliveries", "max_output_bytes", "duration_seconds",
                "delivery_rate_per_second",
            )
        )
    return False


def _effective_limits(selection: WebPolicySelection) -> GatedLimits | None:
    if selection.kali_limits is not None:
        return selection.kali_limits
    preset = selection.kali_limits_preset
    if preset == "probe" or preset == "standard":
        return SUPPORTED_GATED_PRESETS[preset]
    return None


async def _networks(selection: WebPolicySelection, config: EngagementConfig) -> list[str]:
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


@dataclass(frozen=True)
class EngagementStartupSelection:
    """One request-local selection shared by the web host and CLI assembly."""

    environment: dict[str, str]
    engagement: EngagementConfig
    network_policy_path: str | None = None
    network_policy_hash: str | None = None
    network_mode: str = "inherit"


def _canonical_json_hash(value: object) -> str:
    from redteam.core.convert import digest_payload

    return digest_payload(value)


def _preparation_environment_identity(environment: Mapping[str, str]) -> dict[str, str | None]:
    """Bind the chosen Docker context and shell policy content into selection identity."""
    shell_path = environment.get("REDTEAM_SHELL_POLICY_PATH", "").strip() or None
    shell_hash: str | None = None
    if shell_path is not None:
        try:
            shell_hash = hashlib.sha256(Path(shell_path).read_bytes()).hexdigest()
        except OSError:
            raise ValueError("kali_shell_policy_unavailable") from None
    return {
        "docker_context": environment.get("DOCKER_CONTEXT", "").strip() or None,
        "docker_host": environment.get("DOCKER_HOST", "").strip() or None,
        "shell_policy_path": shell_path,
        "shell_policy_sha256": shell_hash,
    }


def _prepared_policy_for_selection(
    selection: WebPolicySelection, targets: Mapping[str, list[str]],
) -> GatedNetworkPolicy | GatedNetworkPolicyRequest:
    assert selection.kali_network_mode == "prepare"
    endpoints: dict[str, list[tuple[int, str]]] = {}
    for item in selection.kali_endpoints:
        host = urlsplit(item.origin).hostname
        assert host is not None
        endpoints.setdefault(host.lower(), []).append((item.port, item.transport))
    sidecar: str | ManagedSidecarRequest
    if selection.kali_sidecar_mode == "managed":
        sidecar = ManagedSidecarRequest()
    else:
        assert selection.kali_sidecar_image is not None
        sidecar = selection.kali_sidecar_image
    preset = selection.kali_limits_preset
    custom_limits = selection.kali_limits
    if preset == "custom":
        preset = None
    return build_gated_network_policy(
        targets=targets,
        sidecar=sidecar,
        preset=preset,
        limits=custom_limits,
        endpoints=endpoints,
    )


async def _resolve_gated_target_pins(
    selection: WebPolicySelection, config: EngagementConfig,
) -> dict[str, list[str]]:
    """Resolve only bounded IPv4 metadata and apply the engagement's denials."""
    from redteam.capability.deployment import check_origin_scope

    resolved: dict[str, list[str]] = {}
    selected_ports: dict[str, set[int]] = {}
    for endpoint in selection.kali_endpoints:
        host = (urlsplit(endpoint.origin).hostname or "").lower()
        selected_ports.setdefault(host, set()).add(endpoint.port)
    loop = asyncio.get_running_loop()
    for origin in selection.origins:
        check_origin_scope(origin, config.scope)
        parts = urlsplit(origin)
        host = (parts.hostname or "").lower()
        if not host:
            raise ValueError("kali_target_origin_invalid")
        for port in selected_ports.get(host, set()):
            allowed, _reasons, _severity = check_target_scope(
                EndpointTarget(host=host, port=port), config.scope,
            )
            if not allowed:
                raise ValueError("kali_endpoint_outside_scope")
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            try:
                entries = await asyncio.wait_for(
                    loop.getaddrinfo(
                        host,
                        parts.port or (443 if parts.scheme == "https" else 80),
                        family=socket.AF_INET,
                        type=socket.SOCK_STREAM,
                    ),
                    timeout=5,
                )
            except (OSError, ValueError, asyncio.TimeoutError):
                raise ValueError("kali_target_dns_resolution_failed") from None
            addresses = sorted({str(ipaddress.IPv4Address(row[4][0])) for row in entries})
        else:
            if not isinstance(literal, ipaddress.IPv4Address):
                raise ValueError("kali_target_ipv4_required")
            addresses = [str(literal)]
        if not addresses:
            raise ValueError("kali_target_dns_no_addresses")
        for address in addresses:
            parsed = ipaddress.IPv4Address(address)
            if parsed.is_link_local or parsed.is_multicast or parsed.is_unspecified:
                raise ValueError("kali_target_prohibited_address")
            for port in selected_ports.get(host, set()):
                denied, _reasons, _severity = check_target_scope(
                    EndpointTarget(host=address, port=port),
                    config.scope,
                    denials_only=True,
                )
                if not denied:
                    raise ValueError("kali_target_address_denied")
        resolved[host] = addresses
    return resolved


def _atomic_policy_artifact(root: Path, policy: GatedNetworkPolicy | GatedNetworkPolicyRequest):
    content = (json.dumps(
        policy.model_dump(mode="json"), sort_keys=True, ensure_ascii=True, separators=(",", ":")
    ) + "\n").encode("utf-8")
    fingerprint = policy_content_hash(policy)
    destination = root / f"gated-network-{fingerprint}.json"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=root, prefix=".gated-network-", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        try:
            os.link(temporary, destination)
        except FileExistsError:
            if not destination.is_file() or policy_content_hash(
                _load_prepared_policy(destination)
            ) != fingerprint:
                raise ValueError("kali_network_artifact_hash_mismatch") from None
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass
    return destination, fingerprint


def _load_prepared_policy(path: Path) -> GatedNetworkPolicy | GatedNetworkPolicyRequest:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("kali_network_artifact_invalid")
    if "managed_sidecar" in data:
        return GatedNetworkPolicyRequest.model_validate(data)
    return GatedNetworkPolicy.model_validate(data)


def _validate_recorded_preparation(
    config: EngagementConfig,
    selection: WebPolicySelection,
    policy: GatedNetworkPolicy | GatedNetworkPolicyRequest,
    current_targets: Mapping[str, list[str]],
    root: Path,
    environment: Mapping[str, str],
    *,
    required: bool,
) -> tuple[Path, str, dict[str, str]]:
    raw = config.metadata.get(GATED_PREPARATION_KEY)
    selection_hash = _canonical_json_hash({
        "selection": selection.model_dump(mode="json"),
        "scope": config.scope.model_dump(mode="json"),
        "execution_environment": _preparation_environment_identity(environment),
    })
    if raw is None:
        if required:
            raise ValueError("kali_preparation_record_missing")
        expected = policy
        destination, request_hash = _atomic_policy_artifact(root, expected)
        record = {
            "version": 1,
            "selection_sha256": selection_hash,
            "request_sha256": request_hash,
            "artifact_path": str(destination.resolve()),
        }
        return destination, request_hash, record
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise ValueError("kali_preparation_record_invalid")
    artifact_raw = raw.get("artifact_path")
    if not isinstance(artifact_raw, str):
        raise ValueError("kali_preparation_record_invalid")
    artifact = Path(artifact_raw)
    if not artifact.resolve().is_relative_to(root.resolve()):
        raise ValueError("kali_preparation_artifact_location_mismatch")
    if raw.get("selection_sha256") != selection_hash:
        raise ValueError("kali_preparation_selection_or_environment_changed")
    recorded = _load_prepared_policy(artifact)
    request_hash = policy_content_hash(recorded)
    if raw.get("request_sha256") != request_hash:
        raise ValueError("kali_preparation_artifact_hash_mismatch")
    # Fresh bounded DNS metadata must reproduce the exact pins and operator fields.
    if policy_content_hash(policy) != request_hash:
        raise ValueError("kali_preparation_dns_or_selection_changed")
    recorded_targets = {
        name: list(addresses) for name, addresses in recorded.registry.targets.items()
    }
    if recorded_targets != {name: list(addresses) for name, addresses in current_targets.items()}:
        raise ValueError("kali_preparation_dns_or_selection_changed")
    return artifact, request_hash, dict(raw)


async def select_engagement_startup(
    engagement: EngagementConfig | Path | str,
    *,
    engagement_path: Path | str | None = None,
    base_environment: Mapping[str, str] | None = None,
    require_recorded_preparation: bool = False,
) -> EngagementStartupSelection:
    """Resolve a selected engagement's request-local startup environment.

    Form preview/save and read-only views do not call this function. It is used
    only at execution startup, before MCP/Kali assembly, and never writes the
    process environment or a dotenv file.
    """
    from redteam.app.web.engagement import EngagementStore

    path: Path | None
    if isinstance(engagement, (str, Path)):
        path = Path(engagement)
        config = load_engagement_config(path)
    else:
        config = engagement
        path = Path(engagement_path) if engagement_path is not None else None
    raw = config.metadata.get(WEB_POLICY_KEY)
    environment = env_snapshot(base_environment)
    if raw is None:
        return EngagementStartupSelection(environment, config)
    selection = WebPolicySelection.model_validate(raw)
    if selection.mode == "manual":
        return EngagementStartupSelection(
            environment, config, network_mode=selection.kali_network_mode,
        )
    if (selection.kali_network_mode == "prepare" and require_recorded_preparation
            and (config.metadata.get(GATED_PREPARATION_KEY) is None
                 or config.metadata.get("mcp_deployment") is None)):
        raise ValueError("kali_preparation_record_missing")
    root = (path.resolve().parent / (path.stem + ".mcp")) if path is not None else None
    if config.metadata.get("mcp_deployment") is not None:
        binding = DeploymentBinding.model_validate(config.metadata["mcp_deployment"])
        if root is None:
            # A resumed CLI build may only have the serialized engagement.
            # Recover its existing .mcp root from the hash-bound deployment
            # directory; if this is a preparation Run, require its immutable
            # request artifact to live under that same root.
            root = Path(binding.source_path).resolve().parent.parent
            path = root.with_suffix(".yaml")
        # Only artifacts in this engagement's generated directory are reused.
        for deployment_artifact in (binding.source_path, binding.policy_path):
            if not Path(deployment_artifact).resolve().is_relative_to(root.resolve()):
                raise ValueError("web_policy_artifact_location_mismatch")
        spec = DeploymentSpec.model_validate_json(Path(binding.source_path).read_bytes())
        wanted = selection.spec(list(spec.policy.allowed_networks))
        if encoded(wanted.model_dump(mode="json")) != encoded(spec.model_dump(mode="json")):
            raise ValueError("web_policy_selection_changed")
        synchronized = synchronize_deployment(config, list(spec.roles), check_environment=False)
        if synchronized != config:
            raise ValueError("web_policy_declarations_changed")
    else:
        if root is None:
            raise ValueError("web_policy_artifact_location_required")
        if path is None:
            raise ValueError("web_policy_artifact_location_required")
        # Scope is proved before DNS resolution; no request to a target is made.
        for origin in selection.origins:
            check_origin_scope(origin, config.scope)
        networks = await _networks(selection, config)
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
        config = load_engagement_config(path)
    environment = deployment_environment(spec, binding, base=environment)
    network_policy_path: str | None = None
    network_policy_hash: str | None = None
    if selection.kali_network_mode == "closed":
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
        network_policy_path = str(destination)
        network_policy_hash = fingerprint
    elif selection.kali_network_mode == "prepare":
        if root is None:
            raise ValueError("web_policy_artifact_location_required")
        if path is None:
            raise ValueError("web_policy_artifact_location_required")
        if selection.kali_docker_context is not None:
            shell_policy_path = environment.get("REDTEAM_SHELL_POLICY_PATH", "").strip()
            if shell_policy_path:
                shell_policy = load_shell_workroom_policy(shell_policy_path)
                if (shell_policy.docker_context is not None
                        and shell_policy.docker_context != selection.kali_docker_context):
                    raise ValueError("kali_docker_context_conflict")
            environment["DOCKER_CONTEXT"] = selection.kali_docker_context
        targets = await _resolve_gated_target_pins(selection, config)
        request = _prepared_policy_for_selection(selection, targets)
        artifact, network_policy_hash, record = _validate_recorded_preparation(
            config,
            selection,
            request,
            targets,
            root,
            environment,
            required=require_recorded_preparation,
        )
        if config.metadata.get(GATED_PREPARATION_KEY) is None:
            EngagementStore(path).install_gated_preparation(config, record)
            config = load_engagement_config(path)
        network_policy_path = str(artifact)
        environment["REDTEAM_GATED_NETWORK_POLICY_PATH"] = network_policy_path
    return EngagementStartupSelection(
        environment,
        config,
        network_policy_path=network_policy_path,
        network_policy_hash=network_policy_hash,
        network_mode=selection.kali_network_mode,
    )


async def prepare_web_deployment(path: Path) -> dict[str, str]:
    """Compatibility wrapper for existing web-host and test callers."""
    return (await select_engagement_startup(path)).environment
