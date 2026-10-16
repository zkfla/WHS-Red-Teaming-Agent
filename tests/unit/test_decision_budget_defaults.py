import pytest
from pydantic import ValidationError

from redteam.app.cli.conversation import ConversationAssistant
from redteam.core.config import OrchestrationConfig
from redteam.orchestration.settings import OrchestrationSettings


def test_orchestration_decision_budget_defaults_and_identity_payloads():
    config = OrchestrationConfig()
    settings = OrchestrationSettings()

    assert config.decision_max_attempts == settings.decision_max_attempts == 12
    assert config.decision_normal_limit == settings.decision_normal_limit == 24
    assert settings.decision_timeout_seconds == 900.0
    assert settings.llm_attempt_timeout_seconds == 180.0
    for payload in (settings.central_payload(), settings.durable_payload()):
        assert payload["decision_max_attempts"] == 12
        assert payload["decision_normal_limit"] == 24
        assert payload["decision_timeout_seconds"] == 900.0
        assert payload["llm_attempt_timeout_seconds"] == 180.0


def test_explicit_previous_defaults_roundtrip_through_config_and_saved_identity():
    config = OrchestrationConfig.from_environment({
        "REDTEAM_DECISION_NORMAL_LIMIT": "12",
        "REDTEAM_DECISION_MAX_ATTEMPTS": "6",
    })
    settings = OrchestrationSettings.from_config(config)

    assert (config.decision_normal_limit, config.decision_max_attempts) == (12, 6)
    for serializer in ("central_payload", "durable_payload"):
        payload = getattr(settings, serializer)()
        assert (payload["decision_normal_limit"], payload["decision_max_attempts"]) == (12, 6)
        restored = OrchestrationSettings.model_validate(payload)
        assert getattr(restored, serializer)() == payload


def test_from_config_copies_validated_decision_budget():
    config = OrchestrationConfig(
        decision_normal_limit=23,
        decision_max_attempts=4,
        decision_timeout_seconds=777.0,
        llm_attempt_timeout_seconds=222.0,
    )

    settings = OrchestrationSettings.from_config(config)

    assert settings.decision_normal_limit == 23
    assert settings.decision_max_attempts == 4
    assert settings.decision_timeout_seconds == 777.0
    assert settings.llm_attempt_timeout_seconds == 222.0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("decision_timeout_seconds", float("inf")),
        ("decision_timeout_seconds", float("nan")),
        ("llm_attempt_timeout_seconds", float("inf")),
        ("llm_attempt_timeout_seconds", float("nan")),
    ],
)
def test_decision_timeouts_reject_non_finite_values(field, value):
    with pytest.raises(ValidationError):
        OrchestrationSettings(**{field: value})


def test_conversation_defaults_use_stage_decision_budget():
    assistant = ConversationAssistant(object())

    assert assistant._timeout_seconds == 180.0
    assert assistant._client._max_attempts == 6
