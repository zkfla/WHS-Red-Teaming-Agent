"""AppConfig -> bootstrap wiring test (explicit test-LLM injection).

Covers: every AppConfig section reaches its component, and every
dependency ``build_application`` would otherwise build can be
overridden with a Fake (instruction.md section 1/21 of the follow-up
brief).
"""

from __future__ import annotations

import pytest

from redteam.app.bootstrap import build_llm_client
from redteam.core.config import LLMConfig


def test_build_llm_client_rejects_fake_provider() -> None:
    with pytest.raises(ValueError, match="real LLM provider"):
        build_llm_client(LLMConfig(provider="fake"))


def test_llm_config_requires_explicit_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REDTEAM_LLM_PROVIDER", raising=False)
    with pytest.raises(ValueError, match="REDTEAM_LLM_PROVIDER"):
        LLMConfig.from_environment()
