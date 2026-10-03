"""Composition-only synchronization of approved MCP deployment and Agent declarations."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from redteam.capability.deployment import (
    ROLE_OPERATIONS,
    DeploymentSpec,
    digest,
    encoded,
    role_args,
)
from redteam.capability.settings import MCPServerSettings, MCPTransportKind
from redteam.control.scope import check_scope
from redteam.core.config import EngagementConfig
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.engagement import EngagementScope
from redteam.core.contracts.target import UrlTarget
from redteam.core.ids import ActionId, AgentId, EngagementId, RunId


class DeploymentBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_path: str
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    policy_path: str
    policy_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def absolute_paths(self) -> DeploymentBinding:
        if any(not Path(p).is_absolute() for p in (self.source_path, self.policy_path)):
            raise ValueError("deployment_absolute_paths_required")
        return self


def check_deployment_scope(spec: DeploymentSpec, scope: EngagementScope) -> None:
    for target in spec.policy.targets.values():
        proposal = ActionProposal(
            action_id=ActionId("deployment-check"),
            run_id=RunId("deployment-check"),
            engagement_id=EngagementId("deployment-check"),
            agent_id=AgentId("deployment-check"),
            capability="web",
            operation="http_request",
            target=UrlTarget(url=target.origin),
            arguments={},
            reason="Operator deployment validation only; never executed",
            created_at=datetime.now(timezone.utc),
        )
        allowed, _, _ = check_scope(proposal, scope)
        if not allowed:
            # Never strip ports to broaden scope; retain the current Control semantics.
            raise ValueError("deployment_origin_outside_control_scope")
    _declared_host_seeds(spec, scope)


def _declared_host_seeds(spec: DeploymentSpec, scope: EngagementScope) -> dict[str, list[str]]:
    """Map policy hostnames to existing scope seeds without adding a bare host."""
    declared = {entry.strip().lower() for entry in (*scope.allowed_hosts, *scope.allowed_domains)}
    seeds: dict[str, list[str]] = {}
    for host, ref in spec.host_bindings.items():
        origin = urlsplit(spec.policy.targets[ref].origin)
        port = origin.port or (443 if origin.scheme == "https" else 80)
        matches = []
        for entry in sorted(declared):
            if entry == host:
                matches.append(entry)
                continue
            try:
                parsed = urlsplit("//" + entry)
                if (
                    parsed.hostname == host
                    and parsed.port == port
                    and not parsed.username
                    and not parsed.password
                    and not parsed.path
                    and not parsed.query
                    and not parsed.fragment
                ):
                    matches.append(entry)
            except ValueError:
                continue  # Malformed entries never create a seed binding.
        if not matches:
            raise ValueError("deployment_origin_outside_control_scope_or_explicit_host_seed")
        seeds[host] = matches
    return seeds


def deployment_metadata(spec: DeploymentSpec, scope: EngagementScope) -> dict:
    """Keep target/session aliases on the exact approved Graph seed identities."""
    check_deployment_scope(spec, scope)
    public = spec.public_metadata()
    seeds = _declared_host_seeds(spec, scope)
    public["target_refs"] = {
        seed: ref for host, ref in spec.host_bindings.items() for seed in seeds[host]
    }
    public["auth_sessions"] = {
        seed: public["auth_sessions"][host] for host in spec.host_bindings for seed in seeds[host]
    }
    return public


def synchronize_deployment(
    engagement: EngagementConfig, server_ids: list[str], *, check_environment: bool = True
) -> EngagementConfig:
    raw = engagement.metadata.get("mcp_deployment")
    if raw is None:
        return engagement  # legacy/manual/remote deployments remain explicit and unchanged
    try:
        binding = DeploymentBinding.model_validate(raw)
        source_bytes = Path(binding.source_path).read_bytes()
        policy_bytes = Path(binding.policy_path).read_bytes()
        if (
            digest(source_bytes) != binding.source_sha256
            or digest(policy_bytes) != binding.policy_sha256
        ):
            raise ValueError("deployment_artifact_digest_mismatch")
        spec = DeploymentSpec.model_validate_json(source_bytes)
        if encoded(spec.server_policy().model_dump(mode="json")) != policy_bytes:
            raise ValueError("deployment_policy_source_mismatch")
        public = deployment_metadata(spec, engagement.scope)
        for key, value in public.items():
            if key in engagement.metadata and engagement.metadata[key] != value:
                raise ValueError("deployment_agent_metadata_mismatch")
        if not set(spec.roles).issubset(server_ids):
            raise ValueError("deployment_required_servers_missing")
        if check_environment:
            for role in spec.roles:
                settings = MCPServerSettings.from_environment(role)
                expected_kind = "web_api" if role == "web-http" else "recon"
                if (
                    settings.transport != MCPTransportKind.STDIO
                    or not settings.command
                    or not Path(settings.command).is_absolute()
                    or Path(settings.command).parent.resolve()
                    != Path(sys.executable).parent.resolve()
                    or Path(settings.command).resolve() != Path(sys.executable).resolve()
                    or settings.max_concurrency != 1
                    or settings.max_output_bytes != 262144
                    or set(settings.process_environment)
                    != {
                        session.bearer_env
                        for session in spec.policy.sessions.values()
                        if session.bearer_env and role == "web-http"
                    }
                    or settings.kind.value != expected_kind
                    or settings.args
                    != tuple(role_args(role, binding.policy_path, binding.policy_sha256))
                    or settings.allowed_tools is None
                    or set(settings.allowed_tools) != set(ROLE_OPERATIONS[role])
                    or settings.tool_operations
                    != {
                        name: tuple(operations)
                        for name, operations in ROLE_OPERATIONS[role].items()
                    }
                    or settings.risk_level.value
                    not in (
                        {"high", "critical"}
                        if role == "web-http"
                        else {"medium", "high", "critical"}
                    )
                ):
                    raise ValueError("deployment_transport_settings_mismatch")
        return engagement.model_copy(update={"metadata": {**engagement.metadata, **public}})
    except (ValueError, OSError):
        # Do not expose input_value, environment contents or a credential-file path via CLI errors.
        raise ValueError(
            "MCP deployment validation failed; approved settings are missing or inconsistent"
        ) from None
