"""Confirmed web scope -> immutable local MCP deployment, before any model/tool.

Uses the capability-owned schema, generator and role metadata. Generated MCP
settings are session inputs; neither process environment nor .env is modified.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import socket
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from redteam.app.mcp_deployment import DeploymentBinding, synchronize_deployment
from redteam.capability.deployment import (
    DeploymentSpec,
    check_origin_scope,
    connection_environment,
    deployment_metadata,
    digest,
    encoded,
    provision,
)
from redteam.capability.server_roles import role_spec
from redteam.capability.servers.config import ServerPolicy, TargetProfile
from redteam.capability.settings import MCPServerSettings, mcp_server_ids_from_environment
from redteam.core.config import EngagementConfig, load_engagement_config
from redteam.core.environment import env_snapshot

WEB_POLICY_KEY = "web_policy"
DEPLOYMENT_KEYS = frozenset({"mcp_deployment", "mcp_targets", "target_refs", "auth_sessions"})


class WebPolicySelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    mode: Literal["site", "paths"]
    origins: list[str] = Field(min_length=1, max_length=30)
    paths: list[str] = Field(default_factory=list, max_length=100)

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
        return self

    def spec(self, networks: list[str]) -> DeploymentSpec:
        targets, bindings = {}, {}
        for origin in self.origins:
            host = urlsplit(origin).hostname
            assert host is not None
            ref = "target_" + hashlib.sha256(origin.encode()).hexdigest()[:16]
            targets[ref] = TargetProfile(
                origin=origin, allowed_paths=self.paths, allow_all_paths=self.mode == "site",
            )
            bindings[host] = ref
        return DeploymentSpec(policy=ServerPolicy(targets=targets, allowed_networks=networks),
                              host_bindings=bindings)


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


async def prepare_web_deployment(path: Path) -> dict[str, str]:
    """Generate once, then validate/reuse the exact source and policy on restart."""
    from redteam.app.web.engagement import EngagementStore

    config = load_engagement_config(path)
    raw = config.metadata.get(WEB_POLICY_KEY)
    if raw is None:
        return env_snapshot()  # Explicit manual/remote configuration stays selected.
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
    return deployment_environment(spec, binding)
