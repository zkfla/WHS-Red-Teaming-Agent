import json
from pathlib import Path

import pytest

from whs_redteam.audit import AuditLogger
from whs_redteam.config import EngagementConfig
from whs_redteam.exceptions import ScopeViolation
from whs_redteam.policy.scope import ScopeGuard


@pytest.fixture
def config() -> EngagementConfig:
    return EngagementConfig(
        name="test",
        targets=("https://app.example.test:8443/base",),
        excluded_paths=("/base/logout", "/base/api/delete"),
        allowed_methods=frozenset({"GET", "HEAD"}),
    )


def test_allows_exact_origin_and_base_path(config: EngagementConfig) -> None:
    decision = ScopeGuard(config).authorize(
        "https://app.example.test:8443/base/products?id=1", "GET"
    )
    assert decision.allowed is True


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.test:8443/base",
        "https://app.example.test/base",
        "http://app.example.test:8443/base",
        "https://app.example.test:8443/outside",
        "https://app.example.test.evil:8443/base",
    ],
)
def test_blocks_out_of_scope_origins_and_paths(config: EngagementConfig, url: str) -> None:
    with pytest.raises(ScopeViolation):
        ScopeGuard(config).authorize(url)


def test_blocks_excluded_path_after_normalization(config: EngagementConfig) -> None:
    guard = ScopeGuard(config)
    with pytest.raises(ScopeViolation, match="excluded"):
        guard.authorize("https://app.example.test:8443/base/ignored/../api/delete/42")


def test_blocks_disallowed_method(config: EngagementConfig) -> None:
    with pytest.raises(ScopeViolation, match="POST"):
        ScopeGuard(config).authorize("https://app.example.test:8443/base", "POST")


def test_redirect_destination_is_revalidated(config: EngagementConfig) -> None:
    with pytest.raises(ScopeViolation):
        ScopeGuard(config).authorize_redirect("https://outside.example.test/result")


def test_blocks_metadata_endpoint_even_if_configured() -> None:
    metadata_config = EngagementConfig(name="bad", targets=("http://169.254.169.254",))
    with pytest.raises(ScopeViolation, match="metadata"):
        ScopeGuard(metadata_config).authorize("http://169.254.169.254/latest/meta-data")


def test_writes_policy_decision_to_audit_log(
    config: EngagementConfig, tmp_path: Path
) -> None:
    audit_path = tmp_path / "events.jsonl"
    ScopeGuard(config, AuditLogger(audit_path)).evaluate("https://outside.example.test")
    event = json.loads(audit_path.read_text(encoding="utf-8"))
    assert event["type"] == "policy_decision"
    assert event["allowed"] is False


def test_scope_decision_serialization_redacts_secret_query(config: EngagementConfig) -> None:
    decision = ScopeGuard(config).evaluate(
        "https://app.example.test:8443/base?token=secret-value"
    )
    assert "secret-value" not in decision.to_dict()["url"]
