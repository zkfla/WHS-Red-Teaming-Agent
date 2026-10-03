"""SDK settings read the supplied snapshot with exact declared aliases."""

import pytest

from redteam.core.config import LLMConfig, extensions_from_environment


def test_explicit_snapshot_preserves_uppercase_aliases(monkeypatch) -> None:
    monkeypatch.setenv("REDTEAM_LLM_PROVIDER", "different-live-value")
    config = LLMConfig.from_environment(
        {
            "REDTEAM_LLM_PROVIDER": "fake",
            "REDTEAM_LLM_MODEL": "fixture",
        }
    )
    assert config.provider == "fake"
    assert config.default_model == "fixture"
    assert extensions_from_environment({"REDTEAM_EXTENSIONS": '["example"]'}) == ["example"]


def test_empty_or_wrong_case_snapshot_does_not_inherit_live_environment(monkeypatch) -> None:
    monkeypatch.setenv("REDTEAM_LLM_PROVIDER", "fake")
    for snapshot in ({}, {"redteam_llm_provider": "fake"}):
        with pytest.raises(ValueError, match="PROVIDER is not set"):
            LLMConfig.from_environment(snapshot)
