"""Operator-only generation of mutually bound policy, declarations and stdio settings.

No MCP tool calls this module. No discovery, login, credential read or scope expansion.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from redteam.capability.server_roles import role_operations, role_spec
from redteam.capability.servers.config import ServerPolicy, SessionProfile, TargetProfile
from redteam.capability.settings import environment_prefix
from redteam.core.config import EngagementConfig, load_engagement_config
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.engagement import EngagementScope
from redteam.core.contracts.target import UrlTarget
from redteam.core.ids import ActionId, AgentId, EngagementId, RunId
from redteam.core.scope_rules import check_scope

ROLE_OPERATIONS = {role: role_operations(role) for role in ("web-http", "recon-passive")}
_REF = re.compile(r"[A-Za-z0-9_.-]{1,80}")


def encoded(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=True, indent=2) + "\n").encode()


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class SessionAccess(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    paths: list[str] = Field(min_length=1, max_length=100)
    expected_access: Literal["allow", "deny", "unspecified"] = "unspecified"


def validate_session_access(
    ref: str, session: SessionProfile, access: SessionAccess, profile: TargetProfile,
) -> None:
    """Shared credential/path validation for declaration preview and deployment."""
    if session.bearer_env and (
        session.bearer_env in {"HOME", "PATH", "SHELL", "USER", "LOGNAME", "TMPDIR"}
        or session.bearer_env.startswith(("PYTHON", "DYLD_", "LD_", "REDTEAM_"))
    ):
        raise ValueError("deployment_reserved_credential_environment_name")
    if not _REF.fullmatch(ref) or not _REF.fullmatch(session.principal_ref):
        raise ValueError("deployment_invalid_session_or_principal_ref")
    if len(set(access.paths)) != len(access.paths) or any(
        not profile.allows_path(path) for path in access.paths
    ):
        raise ValueError("deployment_session_paths_not_approved")
    if session.allowed_paths is not None and set(session.allowed_paths) != set(access.paths):
        raise ValueError("deployment_session_paths_disagree")


class DeploymentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    request_permissions: Literal["read_only", "testing"] = "read_only"
    policy: ServerPolicy
    host_bindings: dict[str, str] = Field(min_length=1, max_length=30)
    session_access: dict[str, SessionAccess] = Field(default_factory=dict, max_length=60)
    roles: tuple[Literal["web-http", "recon-passive"], ...] = ("web-http", "recon-passive")

    @model_validator(mode="after")
    def exact_bindings(self) -> DeploymentSpec:
        if not self.roles or len(set(self.roles)) != len(self.roles):
            raise ValueError("deployment_roles_must_be_unique")
        if self.policy.active_enabled or self.policy.proxy_replay_enabled:
            raise ValueError("deployment_readonly_profiles_required")
        if set(self.host_bindings.values()) != set(self.policy.targets) or (
            len(set(self.host_bindings.values())) != len(self.host_bindings)
        ):
            raise ValueError("deployment_every_target_requires_one_host_binding")
        if set(self.session_access) != set(self.policy.sessions):
            raise ValueError("deployment_every_session_requires_access_paths")
        if self.policy.sessions and "web-http" not in self.roles:
            raise ValueError("deployment_sessions_require_web_http_role")
        for host, ref in self.host_bindings.items():
            profile = self.policy.targets[ref]
            if len(profile.allowed_paths) > 100:
                raise ValueError("deployment_too_many_paths")
            if host != (urlsplit(profile.origin).hostname or "").lower():
                raise ValueError("deployment_host_binding_must_match_origin_hostname")
            if not _REF.fullmatch(ref):
                raise ValueError("deployment_invalid_target_ref")
            if any("*" in path or "{" in path or "}" in path for path in profile.allowed_paths):
                raise ValueError("deployment_exact_concrete_paths_required")
            if len(set(profile.allowed_paths)) != len(profile.allowed_paths):
                raise ValueError("deployment_duplicate_path")
            if self.request_permissions == "read_only" and (
                set(profile.allowed_methods) - {"GET", "HEAD", "OPTIONS"} or any(
                set(methods) - {"GET", "HEAD", "OPTIONS"}
                for methods in profile.allowed_path_methods.values()
                )
            ):
                raise ValueError("deployment_readonly_methods_required")
        for ref, access in self.session_access.items():
            session = self.policy.sessions[ref]
            profile = self.policy.targets[session.target_ref]
            validate_session_access(ref, session, access, profile)
        return self

    @model_serializer(mode="wrap")
    def compatible_payload(self, handler):
        value = handler(self)
        if self.request_permissions == "read_only":
            value.pop("request_permissions", None)
        return value

    def server_policy(self) -> ServerPolicy:
        value = self.policy.model_dump(mode="json")
        for ref, access in self.session_access.items():
            value["sessions"][ref]["allowed_paths"] = access.paths
        return ServerPolicy.model_validate(value)

    def public_metadata(self) -> dict:
        sessions = {}
        targets = {}
        for host, ref in self.host_bindings.items():
            profile = self.policy.targets[ref]
            sessions[host] = [
                {"session_ref": key, **access.model_dump(mode="json")}
                for key, access in self.session_access.items()
                if self.policy.sessions[key].target_ref == ref
            ]
            targets[ref] = {
                "host": host,
                "origin": profile.origin,
                "allowed_paths": profile.allowed_paths,
                **({"allow_all_paths": True} if profile.allow_all_paths else {}),
                "allowed_methods": profile.allowed_methods,
                "allowed_path_methods": profile.allowed_path_methods,
                "allowed_query_keys": profile.allowed_query_keys,
                "allowed_request_headers": profile.allowed_request_headers,
                **{key: value for key, value in profile.model_dump().items() if key in {
                    "allow_all_query_keys", "allowed_global_query_keys",
                    "allow_all_request_headers", "request_body_enabled", "response_body_max_chars",
                }},
                **({key: getattr(profile, key) for key in (
                    "allow_all_query_keys", "allowed_global_query_keys",
                    "allow_all_request_headers", "request_body_enabled", "response_body_max_chars",
                )} if self.request_permissions == "testing" else {}),
                "sessions": [
                    {
                        **item,
                        "principal_ref": self.policy.sessions[item["session_ref"]].principal_ref,
                    }
                    for item in sessions[host]
                ],
                "verification": "operator_declared_not_login_verified",
            }
        return {
            "target_refs": self.host_bindings,
            "auth_sessions": sessions,
            "mcp_targets": targets,
        }


def role_args(role: str, policy_path: str, policy_digest: str) -> list[str]:
    return [
        "-m",
        "redteam.capability.servers",
        role,
        "--policy",
        policy_path,
        "--policy-sha256",
        policy_digest,
    ]


def connection_environment(spec: DeploymentSpec, policy_path: str, policy_digest: str) -> dict:
    values = {"REDTEAM_MCP_SERVERS": ",".join(spec.roles)}
    for role in spec.roles:
        role_specification = role_spec(role)
        if role_specification is None:
            raise ValueError("deployment_unknown_server_role:" + role)
        prefix = environment_prefix(role)
        values.update(
            {
                f"{prefix}_KIND": role_specification.kind,
                f"{prefix}_TRANSPORT": role_specification.transport,
                f"{prefix}_COMMAND": sys.executable,
                f"{prefix}_ARGS_JSON": json.dumps(role_args(role, policy_path, policy_digest)),
                f"{prefix}_RISK_LEVEL": role_specification.risk_level,
                f"{prefix}_MAX_CONCURRENCY": str(role_specification.max_concurrency),
                f"{prefix}_MAX_OUTPUT_BYTES": str(role_specification.max_output_bytes),
                f"{prefix}_PASSTHROUGH_ENV": ",".join(
                    sorted(
                        {
                            session.bearer_env
                            for session in spec.policy.sessions.values()
                            if session.bearer_env
                            and role_specification.forwards_session_credentials
                        }
                    )
                ),
                f"{prefix}_ALLOWED_TOOLS_JSON": json.dumps(list(ROLE_OPERATIONS[role])),
                f"{prefix}_TOOL_OPERATIONS_JSON": json.dumps(ROLE_OPERATIONS[role]),
            }
        )
    return values


def provision(spec: DeploymentSpec, engagement: EngagementConfig, output: Path) -> Path:
    """Create a NEW private directory; never overwrite an existing engagement/.env."""

    public = deployment_metadata(spec, engagement.scope)
    for key in public:
        if key in engagement.metadata and engagement.metadata[key] != public[key]:
            raise ValueError("deployment_existing_metadata_conflicts:" + key)
    output = output.absolute()
    source_path, policy_path = output / "deployment.json", output / "policy.json"
    source_bytes = encoded(spec.model_dump(mode="json"))
    policy_bytes = encoded(spec.server_policy().model_dump(mode="json"))
    public["mcp_deployment"] = {
        "source_path": str(source_path),
        "source_sha256": digest(source_bytes),
        "policy_path": str(policy_path),
        "policy_sha256": digest(policy_bytes),
    }
    updated = engagement.model_copy(update={"metadata": {**engagement.metadata, **public}})
    env = connection_environment(spec, str(policy_path), digest(policy_bytes))
    files = {
        "deployment.json": source_bytes,
        "policy.json": policy_bytes,
        "engagement.yaml": yaml.safe_dump(
            updated.model_dump(mode="json"), sort_keys=False
        ).encode(),
        "framework.env": (
            "\n".join(f"export {key}={shlex.quote(value)}" for key, value in env.items()) + "\n"
        ).encode(),
    }
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    for name, content in files.items():
        fd = os.open(output / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
    return output / "engagement.yaml"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate operator-approved local MCP deployment")
    parser.add_argument("--source", required=True)
    parser.add_argument("--engagement", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        spec = DeploymentSpec.model_validate_json(Path(args.source).read_bytes())
        path = provision(spec, load_engagement_config(args.engagement), Path(args.output))
    except (ValueError, OSError):
        # Pydantic's input_value could include sensitive malformed input. Never print it.
        parser.exit(
            2, "Deployment rejected; check approved scope, exact paths and session references.\n"
        )
    print(f"Created {path}. No server, login or target request was started.")


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


def check_deployment_scope(spec: DeploymentSpec, scope: EngagementScope) -> None:
    for target in spec.policy.targets.values():
        check_origin_scope(target.origin, scope)
    _declared_host_seeds(spec, scope)


def check_origin_scope(origin: str, scope: EngagementScope) -> None:
    """Validate a declared origin before provisioning or resolving its address."""
    proposal = ActionProposal(
        action_id=ActionId("deployment-check"),
        run_id=RunId("deployment-check"),
        engagement_id=EngagementId("deployment-check"),
        agent_id=AgentId("deployment-check"),
        capability="web",
        operation="http_request",
        target=UrlTarget(url=origin),
        arguments={},
        reason="Operator deployment validation only; never executed",
        created_at=datetime.now(timezone.utc),
    )
    allowed, _, _ = check_scope(proposal, scope)
    if not allowed:
        # Never strip ports to broaden scope; retain the current Control semantics.
        raise ValueError("deployment_origin_outside_control_scope")


if __name__ == "__main__":
    main()
