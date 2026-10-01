"""OrchestrationSettings: the bridge from the shared config contract."""

from __future__ import annotations

from redteam.core.config import OrchestrationConfig
from redteam.orchestration.settings import NODES_PER_TURN, RECURSION_SLACK, OrchestrationSettings


def test_defaults_are_usable_without_any_config() -> None:
    settings = OrchestrationSettings()
    assert settings.max_steps >= 1
    assert settings.checkpointing is True


def test_from_config_reads_both_fields_of_the_shared_contract() -> None:
    settings = OrchestrationSettings.from_config(OrchestrationConfig(max_steps=12, max_retries=5))
    assert (settings.max_steps, settings.max_retries) == (12, 5)


def test_overrides_win_over_config_and_none_is_ignored() -> None:
    settings = OrchestrationSettings.from_config(
        OrchestrationConfig(max_steps=12), max_steps=3, checkpointing=None
    )
    assert settings.max_steps == 3
    assert settings.checkpointing is True


def test_recursion_limit_covers_the_whole_loop() -> None:
    # LangGraph's default of 25 super-steps is under four turns of a
    # seven-node loop, so a real run would abort for no domain reason.
    settings = OrchestrationSettings(max_steps=10)
    assert settings.effective_recursion_limit == 10 * NODES_PER_TURN + RECURSION_SLACK
    assert OrchestrationSettings(max_steps=10, recursion_limit=42).effective_recursion_limit == 42


def test_explicit_llm_first_mode_replaces_legacy_additive_planning() -> None:
    settings = OrchestrationSettings.from_config(
        OrchestrationConfig(planning_mode="llm_first")
    )
    assert settings.planning_mode == "llm_first"
    assert settings.llm_candidate_generation is True


def test_legacy_generation_flag_maps_to_hybrid_mode() -> None:
    settings = OrchestrationSettings.from_config(
        OrchestrationConfig(llm_candidate_generation=True)
    )
    assert settings.planning_mode == "hybrid"
