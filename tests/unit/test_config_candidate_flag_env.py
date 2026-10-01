"""Desired env-var behavior for the deprecated candidate-generation flag.

Uses the real environment-loading entrypoint on the shared config object:
the flag name comes from core/config.py (REDTEAM_ORCHESTRATION_* namespace).
"true" (with or without surrounding whitespace) must normalize to boolean
True; "false" must be rejected, never interpreted as another mode.
These tests pin the mandatory-LLM configuration contract.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from redteam.core.config import OrchestrationConfig

_ENV = "REDTEAM_ORCHESTRATION_LLM_CANDIDATE_GENERATION"


def _isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REDTEAM_ORCHESTRATION_MAX_STEPS", raising=False)
    monkeypatch.delenv("REDTEAM_ORCHESTRATION_MAX_RETRIES", raising=False)
    monkeypatch.delenv(_ENV, raising=False)


def test_env_true_normalizes_to_true(monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate(monkeypatch)
    monkeypatch.setenv(_ENV, "true")
    assert OrchestrationConfig.from_environment().llm_candidate_generation is True


def test_env_true_with_whitespace_normalizes(monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate(monkeypatch)
    monkeypatch.setenv(_ENV, " true ")
    assert OrchestrationConfig.from_environment().llm_candidate_generation is True


@pytest.mark.parametrize("raw", ["false", " false "])
def test_env_false_is_rejected(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    _isolate(monkeypatch)
    monkeypatch.setenv(_ENV, raw)
    with pytest.raises(ValidationError):
        OrchestrationConfig.from_environment()
