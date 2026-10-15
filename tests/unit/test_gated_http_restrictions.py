"""HTTP origin restriction projection from a fully verified MCP deployment."""

import pytest
from pydantic import ValidationError

from redteam.app.mcp_deployment import http_restriction_projection
from redteam.capability.deployment import DeploymentSpec, provision
from redteam.capability.gated_http_restrictions import (
    HttpRestrictionProjection,
    project_deployment,
)
from redteam.core.config import EngagementConfig, load_engagement_config

ROLES = ["web-http", "recon-passive"]


def source(origin="https://127.0.0.1:8443", paths=("/", "/accounts/123")):
    return {
        "policy": {
            "targets": {"bank-test": {"origin": origin, "allowed_paths": list(paths)}},
            "allowed_networks": ["127.0.0.1/32"],
        },
        "host_bindings": {"127.0.0.1": "bank-test"},
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


def project(value=None, *, source_sha="a" * 64, policy_sha="b" * 64):
    spec = DeploymentSpec.model_validate(value if value is not None else source())
    return project_deployment(spec, source_sha256=source_sha, policy_sha256=policy_sha)


def test_projection_uses_scheme_default_port_and_matches_only_that_port():
    https = project()
    assert [(o.host, o.port, o.scheme) for o in https.origins] == [("127.0.0.1", 8443, "https")]
    assert len(https.matches("127.0.0.1", 8443)) == 1
    assert https.matches("127.0.0.1", 443) == ()
    assert https.matches("127.0.0.1", 80) == ()
    assert https.matches("10.0.0.9", 8443) == ()

    http_default = project(source(origin="http://127.0.0.1"))
    assert [o.port for o in http_default.origins] == [80]


def test_restriction_is_immutable_and_canonical():
    origin = project(source(paths=("/b", "/a"))).origins[0]
    assert origin.restriction.allowed_paths == ("/a", "/b")
    with pytest.raises(ValidationError):
        origin.restriction.allowed_paths = ("/c",)


def test_digest_is_stable_and_binds_restriction_and_artifacts():
    base = project()
    assert base.digest == project().digest
    assert base.digest != project(source(paths=("/",))).digest
    assert base.digest != project(source_sha="c" * 64).digest
    assert base.digest != project(policy_sha="c" * 64).digest


def test_projection_rejects_non_canonical_origin_order():
    base = project()
    with pytest.raises(ValidationError):
        HttpRestrictionProjection(
            source_sha256="a" * 64,
            policy_sha256="b" * 64,
            origins=(base.origins[0], base.origins[0]),
            verified=True,
        )


def test_loader_projects_only_after_full_verification(tmp_path):
    spec = DeploymentSpec.model_validate(source())
    path = provision(spec, engagement(), tmp_path / "deployment")
    loaded = load_engagement_config(path)
    binding = loaded.metadata["mcp_deployment"]

    projection = http_restriction_projection(loaded, ROLES, check_environment=False)
    assert projection == project_deployment(
        spec,
        source_sha256=binding["source_sha256"],
        policy_sha256=binding["policy_sha256"],
    )


def test_loader_refuses_tampered_policy_and_missing_servers(tmp_path):
    spec = DeploymentSpec.model_validate(source())
    path = provision(spec, engagement(), tmp_path / "deployment")
    loaded = load_engagement_config(path)
    binding = loaded.metadata["mcp_deployment"]

    with pytest.raises(ValueError, match="validation failed"):
        http_restriction_projection(loaded, [], check_environment=False)

    with open(binding["policy_path"], "ab") as handle:
        handle.write(b" ")
    with pytest.raises(ValueError, match="validation failed"):
        http_restriction_projection(loaded, ROLES, check_environment=False)


def test_unbound_engagement_has_no_projection():
    assert http_restriction_projection(engagement(), ROLES, check_environment=False) is None