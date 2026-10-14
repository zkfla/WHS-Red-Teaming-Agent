import pytest
from pydantic import ValidationError

from redteam.app.cli.conversation import ConversationAssistant
from redteam.core.config import OrchestrationConfig
from redteam.orchestration.settings import OrchestrationSettings


def test_orchestration_decision_budget_defaults_and_identity_payloads():
    settings = OrchestrationSettings()

    assert settings.decision_max_attempts == 6
    assert settings.decision_normal_limit == 12
    assert settings.decision_timeout_seconds == 900.0
    assert settings.llm_attempt_timeout_seconds == 180.0
    for payload in (settings.central_payload(), settings.durable_payload()):
        assert payload["decision_timeout_seconds"] == 900.0
        assert payload["llm_attempt_timeout_seconds"] == 180.0


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
