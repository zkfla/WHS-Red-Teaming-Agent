"""Approved paths/session references have one source; drift never grants permission."""

import json
from pathlib import Path

import pytest

from redteam.app.mcp_deployment import synchronize_deployment
from redteam.capability.deployment import DeploymentSpec, connection_environment, provision
from redteam.capability.servers.config import ServerPolicy
from redteam.capability.servers.network import ToolInputError, session_header
from redteam.core.config import EngagementConfig, load_engagement_config


def source(origin="https://127.0.0.1:8443"):
    return {
        "policy": {
            "targets": {"bank-test": {"origin": origin, "allowed_paths": ["/", "/accounts/123"]}},
            "allowed_networks": ["127.0.0.1/32"],
            "sessions": {
                "alice": {
                    "target_ref": "bank-test",
                    "principal_ref": "alice-user",
                    "bearer_env": "TEST_ALICE",
                },
                "bob": {
                    "target_ref": "bank-test",
                    "principal_ref": "bob-user",
                    "bearer_env": "TEST_BOB",
                },
            },
        },
        "host_bindings": {"127.0.0.1": "bank-test"},
        "session_access": {
            "alice": {"paths": ["/accounts/123"], "expected_access": "allow"},
            "bob": {"paths": ["/accounts/123"], "expected_access": "deny"},
        },
    }


def engagement():
    return EngagementConfig.model_validate(
        {
            "name": "synthetic",
            "scope": {"allowed_hosts": ["127.0.0.1"]},
            "execution_budget": {"max_actions": 10},
            "approval_policy": {"require_approval_for": ["web.response_analysis"]},
        }
    )


def installed(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_ALICE", "synthetic-a")
    monkeypatch.setenv("TEST_BOB", "synthetic-b")
    spec = DeploymentSpec.model_validate(source())
    path = provision(spec, engagement(), tmp_path / "deployment")
    loaded = load_engagement_config(path)
    binding = loaded.metadata["mcp_deployment"]
    for key, value in connection_environment(
        spec, binding["policy_path"], binding["policy_sha256"]
    ).items():
        monkeypatch.setenv(key, value)
    return spec, loaded


def test_generated_settings_preserve_scope_approval_and_secret_indirection(tmp_path, monkeypatch):
    spec, config = installed(tmp_path, monkeypatch)
    resolved = synchronize_deployment(config, list(spec.roles))
    assert resolved.scope == engagement().scope
    assert resolved.approval_policy == engagement().approval_policy
    assert resolved.metadata["auth_sessions"]["127.0.0.1"][1]["expected_access"] == "deny"
    public = json.dumps(resolved.metadata)
    assert "TEST_ALICE" not in public and "bearer_env" not in public
    assert resolved.metadata["mcp_targets"]["bank-test"]["origin"] == "https://127.0.0.1:8443"
    assert resolved.metadata["mcp_targets"]["bank-test"]["allowed_paths"] == ["/", "/accounts/123"]
    for path in (tmp_path / "deployment").iterdir():
        assert path.stat().st_mode & 0o077 == 0
    with pytest.raises(FileExistsError):
        provision(spec, engagement(), tmp_path / "deployment")


@pytest.mark.parametrize(
    "mutation",
    [
        "source",
        "policy",
        "metadata",
        "args",
        "operations",
        "risk",
        "server",
        "command",
        "credentials",
        "concurrency",
    ],
)
def test_drift_is_rejected_before_discovery(tmp_path, monkeypatch, mutation):
    spec, config = installed(tmp_path, monkeypatch)
    binding = config.metadata["mcp_deployment"]
    servers = list(spec.roles)
    if mutation in {"source", "policy"}:
        path = Path(binding[mutation + "_path"])
        path.write_bytes(path.read_bytes() + b" ")
    elif mutation == "metadata":
        config.metadata["auth_sessions"]["127.0.0.1"][1]["paths"] = ["/admin"]
    elif mutation == "args":
        monkeypatch.setenv("REDTEAM_MCP_WEB_HTTP_ARGS_JSON", '["-m", "wrong.server"]')
    elif mutation == "operations":
        monkeypatch.setenv("REDTEAM_MCP_WEB_HTTP_TOOL_OPERATIONS_JSON", "{}")
    elif mutation == "risk":
        monkeypatch.setenv("REDTEAM_MCP_WEB_HTTP_RISK_LEVEL", "low")
    elif mutation == "command":
        monkeypatch.setenv("REDTEAM_MCP_WEB_HTTP_COMMAND", "/bin/echo")
    elif mutation == "credentials":
        monkeypatch.setenv("REDTEAM_MCP_RECON_PASSIVE_PASSTHROUGH_ENV", "TEST_ALICE")
    elif mutation == "concurrency":
        monkeypatch.setenv("REDTEAM_MCP_WEB_HTTP_MAX_CONCURRENCY", "2")
    else:
        servers.remove("web-http")
    with pytest.raises(ValueError, match="MCP deployment validation failed"):
        synchronize_deployment(config, servers)


@pytest.mark.parametrize("mutation", ["outside", "denied", "host_port"])
def test_scope_is_checked_by_control_without_port_stripping(tmp_path, mutation):
    spec = DeploymentSpec.model_validate(source())
    config = engagement()
    if mutation == "outside":
        config.scope.allowed_hosts = ["other.invalid"]
    elif mutation == "denied":
        config.scope.denied_hosts = ["127.0.0.1"]
    else:
        config.scope.allowed_hosts = ["127.0.0.1:8443"]
    with pytest.raises(ValueError, match="outside_control_scope"):
        provision(spec, config, tmp_path / "unused")
    assert not (tmp_path / "unused").exists()


@pytest.mark.parametrize(
    "mutation",
    [
        "wildcard",
        "template",
        "missing_session",
        "session_path",
        "principal",
        "write",
        "active",
        "host",
    ],
)
def test_unsafe_or_inconsistent_source_rejected(mutation):
    value = source()
    if mutation in {"wildcard", "template"}:
        value["policy"]["targets"]["bank-test"]["allowed_paths"].append(
            "/accounts/*" if mutation == "wildcard" else "/accounts/{id}"
        )
    elif mutation == "missing_session":
        value["session_access"].pop("bob")
    elif mutation == "session_path":
        value["session_access"]["bob"]["paths"] = ["/admin"]
    elif mutation == "principal":
        value["policy"]["sessions"]["bob"]["principal_ref"] = "password=private"
    elif mutation == "write":
        value["policy"]["targets"]["bank-test"]["allowed_methods"] = ["POST"]
    elif mutation == "active":
        value["policy"]["active_enabled"] = True
    else:
        value["host_bindings"] = {"other.invalid": "bank-test"}
    with pytest.raises(ValueError):
        DeploymentSpec.model_validate(value)


def test_session_path_enforcement_and_missing_credentials(monkeypatch):
    policy = DeploymentSpec.model_validate(source()).server_policy()
    monkeypatch.setenv("TEST_ALICE", "synthetic-only")
    assert session_header(policy, "bank-test", "alice", "/accounts/123")
    with pytest.raises(ToolInputError, match="session allowlist"):
        session_header(policy, "bank-test", "alice", "/")
    monkeypatch.delenv("TEST_BOB", raising=False)
    with pytest.raises(ToolInputError, match="credential unavailable"):
        session_header(policy, "bank-test", "bob", "/accounts/123")


def test_server_policy_hash_is_checked_on_the_bytes_loaded(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(DeploymentSpec.model_validate(source()).server_policy().model_dump_json())
    with pytest.raises(ValueError, match="digest mismatch"):
        ServerPolicy.load(str(path), expected_sha256="0" * 64)


def test_old_manual_engagement_is_unchanged():
    config = engagement()
    assert synchronize_deployment(config, []) is config


def test_no_implicit_target_seed_from_domain_or_network(tmp_path):
    config = engagement()
    config.scope.allowed_hosts = []
    config.scope.allowed_networks = ["127.0.0.1/32"]
    with pytest.raises(ValueError, match="explicit_host_seed"):
        provision(DeploymentSpec.model_validate(source()), config, tmp_path / "unused")
    assert not (tmp_path / "unused").exists()


@pytest.mark.parametrize(
    "name", ["PATH", "HOME", "PYTHONPATH", "DYLD_LIBRARY_PATH", "REDTEAM_MCP_SERVERS"]
)
def test_credential_references_cannot_override_process_settings(name):
    value = source()
    value["policy"]["sessions"]["alice"]["bearer_env"] = name
    with pytest.raises(ValueError, match="reserved_credential"):
        DeploymentSpec.model_validate(value)
