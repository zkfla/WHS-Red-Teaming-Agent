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


@pytest.mark.parametrize("field,value", [
    ("decision_normal_limit", 0), ("decision_max_attempts", 7),
    ("decision_timeout_seconds", 0), ("decision_timeout_seconds", float("inf")),
    ("llm_attempt_timeout_seconds", -1), ("llm_attempt_timeout_seconds", float("nan")),
])
def test_invalid_reasoning_bounds_are_rejected(field, value):
    with pytest.raises(ValidationError):
        OrchestrationConfig.model_validate({field: value})
