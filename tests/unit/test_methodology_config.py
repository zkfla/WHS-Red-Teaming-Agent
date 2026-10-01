"""Reference config is explicit, bounded and absent by default."""

from __future__ import annotations

import json

import pytest

from redteam.app.cli._shared import load_app_config_for_run
from redteam.core.config import CapabilityConfig, MethodologyConfig


def settings():
    return {
        "workspace_policy_path": "/operator/workspace-policy.json",
        "bundle_policy_path": "/operator/bundle-policy.json",
        "prefix": "methodology",
        "reference_ids": ["m4vic-api"],
    }


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("REDTEAM_METHODOLOGY_CONFIG_JSON", raising=False)
    assert MethodologyConfig.from_environment() is None
    assert CapabilityConfig().methodology is None


def test_explicit_environment_config(monkeypatch):
    monkeypatch.setenv("REDTEAM_METHODOLOGY_CONFIG_JSON", json.dumps(settings()))
    result = MethodologyConfig.from_environment()
    assert result.reference_ids == ("m4vic-api",)
    assert result.read_chars == 8192


@pytest.mark.parametrize("raw", ["", " ", "broken SECRET", "{}", "null", "x" * 16385])
def test_invalid_environment_does_not_echo_value(monkeypatch, raw):
    monkeypatch.setenv("REDTEAM_METHODOLOGY_CONFIG_JSON", raw)
    with pytest.raises(ValueError) as error:
        MethodologyConfig.from_environment()
    assert str(error.value) == "methodology_config_invalid"


@pytest.mark.parametrize(
    "changes",
    [
        {"api_key": "must-not-be-accepted"},
        {"read_chars": 255},
        {"read_chars": 16385},
        {"max_context_chars": 24001},
        {"reference_ids": ["one", "two", "three", "four"]},
    ],
)
def test_operator_config_limits(changes):
    with pytest.raises(ValueError):
        MethodologyConfig.model_validate({**settings(), **changes})


def test_cli_passes_explicit_reference_configuration(tmp_path, monkeypatch):
    engagement = tmp_path / "engagement.yaml"
    engagement.write_text("name: synthetic\nexecution_budget:\n  max_actions: 20\n")
    monkeypatch.setenv("REDTEAM_METHODOLOGY_CONFIG_JSON", json.dumps(settings()))
    monkeypatch.setenv("REDTEAM_LLM_PROVIDER", "fake")
    monkeypatch.delenv("REDTEAM_PLANNING_MODE", raising=False)
    monkeypatch.setenv("REDTEAM_MCP_SERVERS", "workspace-read")
    config = load_app_config_for_run(str(engagement))
    assert config.capability.methodology.reference_ids == ("m4vic-api",)
    assert config.capability.mcp_servers == ["workspace-read"]
    assert config.orchestration.planning_mode == "playbook_only"
