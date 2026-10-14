"""Interactive effort choices reach the same configuration as scripted runs."""

import os

import pytest

from redteam.app.cli.commands import interactive
from tests.unit.test_config_opencode_go import (
    _MANAGED_ENV_VARS,
    _onboarding,
    _ScriptedConsole,
)


@pytest.fixture
def setup_environment(monkeypatch, tmp_path):
    original = dict(os.environ)
    for name in (*_MANAGED_ENV_VARS, "REDTEAM_LLM_REASONING_EFFORT"):
        monkeypatch.delenv(name, raising=False)
    try:
        yield _onboarding(monkeypatch, tmp_path)
    finally:
        os.environ.clear()
        os.environ.update(original)


async def test_interactive_effort_reprompts_then_persists(setup_environment):
    console = _ScriptedConsole(["opencode-go", "openai/example-model", "invalid", "high"])
    config, _, _, _ = await interactive._ensure_runtime_config(console)
    assert config.reasoning_effort == "high"
    assert os.environ["REDTEAM_LLM_REASONING_EFFORT"] == "high"
    assert "REDTEAM_LLM_REASONING_EFFORT=high" in setup_environment.read_text(encoding="utf-8")
    assert "Choose a listed reasoning effort" in " ".join(console.transcript)


async def test_interactive_default_keeps_effort_unset(setup_environment):
    config, _, _, _ = await interactive._ensure_runtime_config(
        _ScriptedConsole(["", "example-model", ""]),
    )
    assert config.reasoning_effort is None
    assert "reasoning_effort" not in config.model_dump()
    assert "REDTEAM_LLM_REASONING_EFFORT" not in setup_environment.read_text(encoding="utf-8")


async def test_existing_effort_is_used_without_an_extra_prompt(setup_environment, monkeypatch):
    monkeypatch.setenv("REDTEAM_LLM_REASONING_EFFORT", "medium")
    console = _ScriptedConsole(["opencode-go", "openai/example-model"])
    config, _, _, _ = await interactive._ensure_runtime_config(console)
    assert config.reasoning_effort == "medium"
    assert not any("Reasoning effort (" in line for line in console.transcript)


async def test_configured_provider_reads_effort_without_setup(monkeypatch):
    monkeypatch.setenv("REDTEAM_LLM_PROVIDER", "litellm")
    monkeypatch.setenv("REDTEAM_LLM_MODEL", "example-model")
    monkeypatch.setenv("REDTEAM_LLM_REASONING_EFFORT", "high")
    config, _, _, _ = await interactive._ensure_runtime_config(_ScriptedConsole([]))
    assert config.reasoning_effort == "high"
