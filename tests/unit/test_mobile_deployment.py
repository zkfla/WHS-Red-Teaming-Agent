"""P3 operator configuration preparation: no real device or model calls."""

import json
import stat
import sys

import pytest
from pydantic import ValidationError

from redteam.capability.mobile_deployment import (
    MobileDeploymentSpec,
    connection_environment,
    deployment_metadata,
    provision,
)
from redteam.capability.servers.mobile import MobileDiagnosticsPolicy, ToolInputError
from redteam.capability.settings import MCPServerSettings
from redteam.core.config import EngagementConfig, load_engagement_config
from redteam.core.contracts.engagement import EngagementScope, ExecutionBudget


def specification():
    return MobileDeploymentSpec(
        devices={
            "test-device": {
                "asset_ref": "test-app",
                "serial": "synthetic-serial",
                "package_name": "org.test.app",
            }
        },
        additional_server_ids=["recon-passive", "web-http"],
    )


def engagement():
    return EngagementConfig(
        name="Synthetic Mobile preparation",
        scope=EngagementScope(allowed_assets=["test-app"]),
        execution_budget=ExecutionBudget(max_actions=3),
    )


def test_mobile_preparation_pins_policy_uses_current_runtime_and_preserves_control(tmp_path):
    spec, original = specification(), engagement()
    result = provision(spec, original, tmp_path / "generated")
    loaded = load_engagement_config(result)
    assert loaded.scope == original.scope
    assert loaded.rules_of_engagement == original.rules_of_engagement
    assert loaded.execution_budget == original.execution_budget
    assert loaded.approval_policy == original.approval_policy
    assert original.metadata == {}
    assert loaded.metadata["device_refs"] == {"test-app": "test-device"}
    assert loaded.metadata["device_serials"] == {"test-app": "synthetic-serial"}
    assert loaded.metadata["package_names"] == {"test-app": "org.test.app"}
    binding = loaded.metadata["mobile_deployment"]
    assert binding["verification"] == "operator_declared_not_device_or_app_verified"
    policy = MobileDiagnosticsPolicy.load(
        binding["policy_path"],
        expected_sha256=binding["policy_sha256"],
    )
    assert policy == spec.server_policy()
    settings = MCPServerSettings.from_environment(
        spec.server_id,
        env=connection_environment(
            spec,
            binding["policy_path"],
            binding["policy_sha256"],
        ),
    )
    assert settings.command == sys.executable
    assert settings.allowed_tools == ("device_shell",)
    assert settings.tool_operations == {"device_shell": ("device_shell",)}
    assert settings.max_concurrency == 1
    assert settings.process_environment == {}
    assert str(result.parent / "policy.json") in settings.args
    assert "web-http" in (result.parent / "framework.env").read_text()
    assert stat.S_IMODE(result.parent.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in result.parent.iterdir())


@pytest.mark.parametrize("deny", [False, True])
def test_generator_does_not_expand_scope(tmp_path, deny):
    original = engagement()
    original.scope.allowed_assets = [] if not deny else ["test-app"]
    original.scope.denied_assets = ["test-app"] if deny else []
    with pytest.raises(ValueError, match="outside_scope"):
        provision(specification(), original, tmp_path / "rejected")
    assert not (tmp_path / "rejected").exists()


@pytest.mark.parametrize(
    "key,value",
    [
        ("device_serial", "another-device"),
        ("device_refs", {"test-app": "other-ref"}),
        ("package_names", []),
    ],
)
def test_existing_metadata_conflict_is_not_overwritten(key, value):
    original = engagement()
    original.metadata[key] = value
    with pytest.raises(ValueError, match="metadata_conflicts"):
        deployment_metadata(specification(), original)
    assert original.metadata[key] == value


def test_existing_directory_and_policy_tampering_fail_closed(tmp_path):
    result = provision(specification(), engagement(), tmp_path / "generated")
    before = result.read_bytes()
    with pytest.raises(FileExistsError):
        provision(specification(), engagement(), result.parent)
    assert result.read_bytes() == before
    binding = load_engagement_config(result).metadata["mobile_deployment"]
    policy = result.parent / "policy.json"
    policy.write_bytes(policy.read_bytes() + b"\n")
    with pytest.raises(ToolInputError, match="digest mismatch"):
        MobileDiagnosticsPolicy.load(str(policy), expected_sha256=binding["policy_sha256"])


@pytest.mark.parametrize(
    "update",
    [
        {"devices": {}},
        {"server_id": "bad;command"},
        {"additional_server_ids": ["mobile-diagnostics"]},
        {"allowed_commands": ["install"]},
        {"max_output_bytes": 0},
    ],
)
def test_invalid_mobile_spec_fails_before_files_or_discovery(update):
    raw = specification().model_dump()
    raw.update(update)
    with pytest.raises((ValidationError, ValueError)):
        MobileDeploymentSpec.model_validate(raw)


def test_ambiguous_installations_and_package_injection_rejected():
    raw = specification().model_dump()
    raw["devices"]["second"] = dict(raw["devices"]["test-device"])
    with pytest.raises(ValidationError, match="ambiguous_installation"):
        MobileDeploymentSpec.model_validate(raw)
    raw["devices"].pop("second")
    raw["devices"]["test-device"]["package_name"] = "org.test.app;input"
    with pytest.raises(ValidationError):
        MobileDeploymentSpec.model_validate(raw)


def test_oversized_and_wrong_digest_policy_rejected_before_device_access(tmp_path):
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(specification().server_policy().model_dump()))
    with pytest.raises(ToolInputError, match="digest mismatch"):
        MobileDiagnosticsPolicy.load(str(policy), expected_sha256="0" * 64)
    policy.write_bytes(b" " * 65537)
    with pytest.raises(ToolInputError, match="oversized"):
        MobileDiagnosticsPolicy.load(str(policy))
