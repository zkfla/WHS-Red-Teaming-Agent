from pathlib import Path

import pytest

from whs_redteam.config import (
    ActorConfig,
    AuthorizationFixture,
    EngagementConfig,
    InjectionFixture,
    load_config,
)
from whs_redteam.exceptions import ConfigurationError


def test_example_config_loads() -> None:
    config = load_config(Path("configs/engagement.example.yaml"))
    assert config.engagement.targets == ("http://localhost:3000",)
    assert config.engagement.allowed_methods == frozenset({"GET", "HEAD", "OPTIONS"})


def test_embedded_credentials_are_rejected() -> None:
    with pytest.raises(ValueError, match="credentials"):
        EngagementConfig(name="bad", targets=("https://user:pass@example.com",))


def test_state_changing_method_requires_explicit_flag() -> None:
    with pytest.raises(ValueError, match="allow_state_change"):
        EngagementConfig(
            name="bad",
            targets=("https://example.test",),
            allowed_methods=frozenset({"GET", "POST"}),
        )


def test_unknown_config_fields_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(
        "engagement:\n  name: bad\n  targets: [https://example.test]\n  typo: true\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError):
        load_config(path)


def test_hostname_with_resolver_rule_delimiters_is_rejected() -> None:
    with pytest.raises(ValueError, match="invalid hostname"):
        EngagementConfig(name="bad", targets=("https://example.test,MAP-attacker",))


def test_discovered_link_following_is_disabled_by_default() -> None:
    config = EngagementConfig(name="safe-default", targets=("https://example.test",))
    assert config.follow_discovered_links is False


def test_actor_only_accepts_allowlisted_secret_headers() -> None:
    with pytest.raises(ValueError, match="not allowed"):
        ActorConfig(actor_id="user", header_env={"X-Unsafe": "WHS_UNSAFE_VALUE"})


def test_anonymous_actor_identifier_is_reserved() -> None:
    with pytest.raises(ValueError, match="reserved"):
        EngagementConfig(
            name="bad-actor",
            targets=("https://example.test",),
            actors=(
                ActorConfig(
                    actor_id="Anonymous",
                    header_env={"Authorization": "WHS_ANON_AUTH"},
                ),
            ),
        )


def test_actor_header_names_are_normalized_and_cannot_repeat() -> None:
    actor = ActorConfig(
        actor_id="user",
        header_env={"Authorization": "WHS_USER_AUTH"},
    )
    assert actor.header_env == {"authorization": "WHS_USER_AUTH"}
    with pytest.raises(ValueError, match="duplicate"):
        ActorConfig(
            actor_id="user",
            header_env={
                "Authorization": "WHS_USER_AUTH",
                "authorization": "WHS_USER_AUTH_2",
            },
        )


def test_actors_cannot_share_a_credential_environment_reference() -> None:
    with pytest.raises(ValueError, match="same credential environment"):
        EngagementConfig(
            name="duplicate-credential-reference",
            targets=("https://example.test",),
            actors=(
                ActorConfig(
                    actor_id="user_a",
                    header_env={"Authorization": "SHARED_AUTH"},
                ),
                ActorConfig(
                    actor_id="user_b",
                    header_env={"Authorization": "SHARED_AUTH"},
                ),
            ),
        )


def test_authorization_fixture_requests_must_stay_in_approval_scope() -> None:
    actors = (
        ActorConfig(
            actor_id="owner",
            header_env={"Authorization": "OWNER_AUTH"},
        ),
        ActorConfig(
            actor_id="other",
            header_env={"Authorization": "OTHER_AUTH"},
        ),
    )
    with pytest.raises(ValueError, match="approval_scope_url"):
        EngagementConfig(
            name="fixture-scope",
            targets=("https://example.test",),
            actors=actors,
            authorization_fixtures=(
                AuthorizationFixture(
                    fixture_id="order-idor",
                    approval_scope_url="https://example.test/api/orders",
                    target_url="https://example.test/api/orders/123",
                    negative_control_url="https://example.test/admin/control",
                    owner_actor="owner",
                    non_owner_actors=("other",),
                ),
            ),
            enabled_capabilities=frozenset(
                {
                    "discovery",
                    "api_schema",
                    "auth_session",
                    "resource_model",
                    "finding_validation",
                    "idor_safe",
                }
            ),
            approval_hmac_secret_env="APPROVAL_SECRET",
        )


def test_injection_fixture_requires_scope_capability_and_integer_baseline() -> None:
    with pytest.raises(ValueError, match="integer baseline_value"):
        InjectionFixture(
            fixture_id="bad-integer",
            approval_scope_url="https://example.test",
            target_url="https://example.test/search",
            parameter="id",
            baseline_value="not-a-number",
            parameter_kind="integer",
            checks=frozenset({"sqli"}),
        )

    fixture = InjectionFixture(
        fixture_id="search-input",
        approval_scope_url="https://example.test/search",
        target_url="https://example.test/admin/search",
        parameter="q",
        baseline_value="safe",
        checks=frozenset({"sqli"}),
    )
    with pytest.raises(ValueError, match="within approval_scope_url"):
        EngagementConfig(
            name="bad-injection-scope",
            targets=("https://example.test",),
            injection_fixtures=(fixture,),
            enabled_capabilities=frozenset({"sqli_safe"}),
            approval_hmac_secret_env="APPROVAL_SECRET",
        )

    in_scope = fixture.model_copy(
        update={"target_url": "https://example.test/search/items"}
    )
    with pytest.raises(ValueError, match="enabled capabilities"):
        EngagementConfig(
            name="missing-injection-capability",
            targets=("https://example.test",),
            injection_fixtures=(in_scope,),
            enabled_capabilities=frozenset({"finding_validation"}),
            approval_hmac_secret_env="APPROVAL_SECRET",
        )


def test_automatic_injection_discovery_requires_capabilities_and_approval() -> None:
    with pytest.raises(ValueError, match="enabled capabilities"):
        EngagementConfig(
            name="auto-missing-capability",
            targets=("https://example.test",),
            auto_injection_discovery=True,
            enabled_capabilities=frozenset({"sqli_safe"}),
            approval_hmac_secret_env="APPROVAL_SECRET",
        )

    with pytest.raises(ValueError, match="approval_hmac_secret_env"):
        EngagementConfig(
            name="auto-missing-approval",
            targets=("https://example.test",),
            auto_injection_discovery=True,
            enabled_capabilities=frozenset({"sqli_safe", "xss_safe"}),
        )

    with pytest.raises(ValueError, match="xss_safe capability"):
        EngagementConfig(
            name="browser-xss-missing-capability",
            targets=("https://example.test",),
            xss_browser_confirmation=True,
        )
