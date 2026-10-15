"""OrchestrationSettings: the bridge from the shared config contract."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from redteam.core.config import OrchestrationConfig
from redteam.orchestration.settings import NODES_PER_TURN, RECURSION_SLACK, OrchestrationSettings


def test_defaults_are_usable_without_any_config() -> None:
    settings = OrchestrationSettings()
    assert settings.max_steps >= 1
    assert settings.checkpointing is True


def test_from_config_reads_the_shared_contract() -> None:
    settings = OrchestrationSettings.from_config(OrchestrationConfig(max_steps=12))
    assert settings.max_steps == 12
    # ``max_retries`` was removed from the public contract (F21): it is not a
    # selectable field and no longer travels through ``from_config``.
    assert "max_retries" not in type(settings).model_fields
    assert "max_retries" not in type(OrchestrationConfig()).model_fields
    assert settings.central_payload()["max_retries"] == 3


def test_overrides_win_over_config_and_none_is_ignored() -> None:
    settings = OrchestrationSettings.from_config(
        OrchestrationConfig(max_steps=12), max_steps=3, checkpointing=None
    )
    assert settings.max_steps == 3
    assert settings.checkpointing is True


def test_recursion_limit_covers_the_whole_loop() -> None:
    # Each native turn checkpoints prepare, model, tools and settle separately.
    settings = OrchestrationSettings(max_steps=10)
    assert settings.effective_recursion_limit == 10 * NODES_PER_TURN + RECURSION_SLACK
    assert OrchestrationSettings(max_steps=10, recursion_limit=42).effective_recursion_limit == 42


def test_llm_reasoning_is_required_in_every_config() -> None:
    settings = OrchestrationSettings.from_config(OrchestrationConfig(llm_candidate_generation=True))
    assert settings.llm_candidate_generation is True
    assert "planning_mode" not in type(settings).model_fields


@pytest.mark.parametrize("model", [OrchestrationConfig, OrchestrationSettings])
def test_disabling_llm_reasoning_is_rejected(model) -> None:
    with pytest.raises(ValidationError):
        model(llm_candidate_generation=False)
