"""Composition-only synchronization of approved MCP deployment and Agent declarations."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from redteam.capability.deployment import (
    ROLE_OPERATIONS,
    DeploymentSpec,
    check_deployment_scope,
    deployment_metadata,
    digest,
    encoded,
    role_args,
)
from redteam.capability.server_roles import role_spec
from redteam.capability.settings import MCPServerSettings, MCPTransportKind
from redteam.core.config import EngagementConfig


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


def synchronize_deployment(
    engagement: EngagementConfig,
    server_ids: list[str],
    *,
    check_environment: bool = True,
    env: Mapping[str, str] | None = None,
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
        check_deployment_scope(spec, engagement.scope)
        public = deployment_metadata(spec, engagement.scope)
        for key, value in public.items():
            if key in engagement.metadata and engagement.metadata[key] != value:
                raise ValueError("deployment_agent_metadata_mismatch")
        if not set(spec.roles).issubset(server_ids):
            raise ValueError("deployment_required_servers_missing")
        if check_environment:
            for role in spec.roles:
                settings = MCPServerSettings.from_environment(role, env=env)
                specification = role_spec(role)
                if specification is None:
                    raise ValueError("deployment_role_not_registered")
                expected_kind = specification.kind
                if (
                    settings.transport != MCPTransportKind.STDIO
                    or not settings.command
                    or not Path(settings.command).is_absolute()
                    or Path(settings.command).parent.resolve()
                    != Path(sys.executable).parent.resolve()
                    or Path(settings.command).resolve() != Path(sys.executable).resolve()
                    or settings.max_concurrency != specification.max_concurrency
                    or settings.max_output_bytes != specification.max_output_bytes
                    or set(settings.process_environment)
                    != {
                        session.bearer_env
                        for session in spec.policy.sessions.values()
                        if session.bearer_env and specification.forwards_session_credentials
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
                        if specification.forwards_session_credentials
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
