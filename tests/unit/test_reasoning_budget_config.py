"""Configured count/time bounds reach the operative reasoning settings."""

import pytest
from pydantic import ValidationError

from redteam.core.config import OrchestrationConfig
from redteam.orchestration.settings import OrchestrationSettings


def test_reasoning_count_and_time_environment_reaches_runtime():
    config = OrchestrationConfig.from_environment({
        "REDTEAM_DECISION_NORMAL_LIMIT": "14",
        "REDTEAM_DECISION_MAX_ATTEMPTS": "5",
        "REDTEAM_DECISION_TIMEOUT_SECONDS": "600",
        "REDTEAM_LLM_ATTEMPT_TIMEOUT_SECONDS": "120",
    })
    settings = OrchestrationSettings.from_config(config)
    assert settings.decision_normal_limit == 14
    assert settings.decision_max_attempts == 5
    assert settings.decision_timeout_seconds == 600
    assert settings.llm_attempt_timeout_seconds == 120
    assert settings.central_payload()["decision_timeout_seconds"] == 600
    defaults = OrchestrationConfig.from_environment({})
    assert (defaults.decision_normal_limit, defaults.decision_max_attempts) == (24, 12)


@pytest.mark.parametrize("field,value", [
    ("decision_normal_limit", 0), ("decision_normal_limit", 65),
    ("decision_max_attempts", 0), ("decision_max_attempts", 33),
    ("decision_timeout_seconds", 0), ("decision_timeout_seconds", float("inf")),
    ("llm_attempt_timeout_seconds", -1), ("llm_attempt_timeout_seconds", float("nan")),
])
def test_invalid_reasoning_bounds_are_rejected(field, value):
    with pytest.raises(ValidationError):
        OrchestrationConfig.model_validate({field: value})
    with pytest.raises(ValidationError):
        OrchestrationSettings.model_validate({field: value})


@pytest.mark.parametrize(("field", "values"), [
    ("decision_normal_limit", [1, 64]),
    ("decision_max_attempts", [1, 32]),
])
def test_reasoning_bounds_accept_declared_endpoints(field, values):
    for value in values:
        assert getattr(OrchestrationConfig.model_validate({field: value}), field) == value
        assert getattr(OrchestrationSettings.model_validate({field: value}), field) == value
