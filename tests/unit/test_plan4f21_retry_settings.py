'''F21: retry knobs are removed from the public contract; historic runs decode.'''

from __future__ import annotations

import pytest
from pydantic import ValidationError

from redteam.core.config import OrchestrationConfig
from redteam.orchestration.settings import OrchestrationSettings


def test_retry_knobs_are_not_selectable_settings() -> None:
    fields = type(OrchestrationSettings()).model_fields
    assert "max_retries" not in fields
    assert "retry_retryable_tool_failures" not in fields
    # The public config/CLI schema no longer advertises them either.
    assert "max_retries" not in type(OrchestrationConfig()).model_fields


def test_original_snapshot_identity_round_trips() -> None:
    settings = OrchestrationSettings.model_validate(
        {"max_retries": 2, "retry_retryable_tool_failures": False}
    )
    assert settings.central_payload()["max_retries"] == 2
    assert settings.central_payload()["retry_retryable_tool_failures"] is False
    assert settings.durable_payload()["max_retries"] == 2


def test_default_identity_is_stable_for_new_runs() -> None:
    assert OrchestrationSettings.from_config().central_payload()["max_retries"] == 3


def test_env_retry_setting_is_explicitly_rejected() -> None:
    env = {"REDTEAM_ORCHESTRATION_MAX_RETRIES": "2"}
    with pytest.raises(ValueError, match="REDTEAM_ORCHESTRATION_MAX_RETRIES"):
        OrchestrationConfig.from_environment(env)


def test_direct_config_with_removed_retry_key_is_rejected() -> None:
    # New API/YAML input is rejected, not silently dropped (extra=ignore).
    with pytest.raises(ValidationError, match="max_retries"):
        OrchestrationConfig.model_validate({"max_steps": 10, "max_retries": 3})
