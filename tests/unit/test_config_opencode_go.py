"""OpenCode Go selection: env parsing, bootstrap wiring, onboarding, offline validate.

T2 surface only: provider `opencode-go` with an explicit SDK-qualified model,
the `OPENCODE_API_KEY` reference, and the additive offline `check_llm` step.
No provider is ever called (a poisoned adapter stub proves the offline path
never builds it); no live keys are used.
"""

from __future__ import annotations

import argparse
import os
import sys
import types
from pathlib import Path

import pytest
from rich.console import Console

from redteam.app.bootstrap import build_llm_client
from redteam.app.cli._shared import EXIT_CONFIG_ERROR, EXIT_OK
from redteam.app.cli.commands import interactive
from redteam.app.cli.commands import validate as validate_command
from redteam.core.config import LLMConfig
from redteam.llm.opencode_go_config import (
    OPENCODE_GO_API_KEY_ENV_VAR,
    is_go_provider,
    validate_model,
)

_MANAGED_ENV_VARS = (
    "REDTEAM_LLM_PROVIDER",
    "REDTEAM_LLM_MODEL",
    "REDTEAM_LLM_API_KEY_ENV_VAR",
    "LLM_API_KEY",
    "OPENCODE_API_KEY",
    "MY_CUSTOM_KEY",
    "REDTEAM_MCP_SERVERS",
    "REDTEAM_ORCHESTRATION_MAX_STEPS",
    "REDTEAM_ORCHESTRATION_MAX_RETRIES",
    "REDTEAM_ORCHESTRATION_LLM_CANDIDATE_GENERATION",
    "REDTEAM_RESOURCE_WAIT_TIMEOUT_SECONDS",
    "REDTEAM_SPECIALIST_PROFILE",
    "REDTEAM_CODE_FIXTURE_POLICY_PATH",
    "REDTEAM_METHODOLOGY_CONFIG_JSON",
    "REDTEAM_EXTENSIONS",
    "REDTEAM_PLANNING_PROVIDER",
    "REDTEAM_SHELL_POLICY_PATH",
    "REDTEAM_GATED_NETWORK_POLICY_PATH",
    "REDTEAM_LLM_REASONING_EFFORT",
    "REDTEAM_LLM_CYBER_ACCESS_PROGRAM",
)


@pytest.fixture
def _isolated_env():
    """Snapshot/restore os.environ: onboarding writes it directly."""
    before = dict(os.environ)
    for var in _MANAGED_ENV_VARS:
        os.environ.pop(var, None)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(before)


class _ScriptedConsole(Console):
    def __init__(self, answers: list[str]) -> None:
        super().__init__()
        self._answers = list(answers)
        self.transcript: list[str] = []

    def print(self, *args: object, **kwargs: object) -> None:
        self.transcript.append(" ".join(str(arg) for arg in args))

    def input(self, prompt: object = "", *args: object, **kwargs: object) -> str:
        self.transcript.append(str(prompt))
        if not self._answers:
            raise EOFError("no more scripted answers")
        return self._answers.pop(0)


def _onboarding(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    env_file = tmp_path / ".env"
    monkeypatch.setattr(interactive, "DEFAULT_ENV_PATH", env_file)
    monkeypatch.setattr(interactive, "prompt_secret", lambda label: "sk-test-key")
    return env_file


_ENGAGEMENT_YAML = """
name: "go-check"
scope:
  allowed_hosts: ["a.local"]
execution_budget:
  max_actions: 1
rate_limit_policy:
  max_actions_per_minute: 10
approval_policy:
  require_approval_for: []
"""


def _engagement(tmp_path: Path) -> Path:
    path = tmp_path / "engagement.yaml"
    path.write_text(_ENGAGEMENT_YAML, encoding="utf-8")
    return path


def _validate_args(path: Path) -> argparse.Namespace:
    os.environ["REDTEAM_LLM_REQUEST_POLICY_JSON"] = (
        '{"mode":"bounded","max_requests":10}'
    )
    return argparse.Namespace(engagement_path=str(path), json=False)


def _stub_go_adapter(monkeypatch: pytest.MonkeyPatch, sentinel: object) -> dict:
    module = types.ModuleType("redteam.llm.opencode_go")
    calls: dict = {}

    def build(config):  # noqa: ANN001, ANN202
        calls["config"] = config
        return sentinel

    module.build = build  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "redteam.llm.opencode_go", module)
    return calls


def _stub_litellm(monkeypatch: pytest.MonkeyPatch) -> dict:
    module = types.ModuleType("redteam.llm.litellm")
    calls: dict = {}

    class LiteLLMClient:
        def __init__(self, model=None, api_key=None, **kwargs):  # noqa: ANN001, ANN003
            calls.update(model=model, api_key=api_key, kwargs=kwargs)

    module.LiteLLMClient = LiteLLMClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "redteam.llm.litellm", module)
    return calls


def _poison_go_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    module = types.ModuleType("redteam.llm.opencode_go")

    def build(config):  # noqa: ANN001, ANN202
        raise AssertionError("offline validate must never build the Go adapter")

    module.build = build  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "redteam.llm.opencode_go", module)


# --- env parsing ---


def test_opencode_go_env_round_trip_openai_prefix() -> None:
    config = LLMConfig.from_environment(
        {"REDTEAM_LLM_PROVIDER": "opencode-go", "REDTEAM_LLM_MODEL": "openai/gpt-x"}
    )
    assert (config.provider, config.default_model) == ("opencode-go", "openai/gpt-x")


def test_opencode_go_env_round_trip_responses_prefix() -> None:
    config = LLMConfig.from_environment(
        {
            "REDTEAM_LLM_PROVIDER": "opencode-go",
            "REDTEAM_LLM_MODEL": "openai/responses/gpt-x",
        }
    )
    assert config.default_model == "openai/responses/gpt-x"


def test_opencode_go_env_round_trip_anthropic_prefix() -> None:
    config = LLMConfig.from_environment(
        {"REDTEAM_LLM_PROVIDER": "opencode-go", "REDTEAM_LLM_MODEL": "anthropic/cl-x"}
    )
    assert config.default_model == "anthropic/cl-x"


def test_opencode_go_preserves_api_key_env_var_name() -> None:
    config = LLMConfig.from_environment(
        {
            "REDTEAM_LLM_PROVIDER": "opencode-go",
            "REDTEAM_LLM_MODEL": "openai/gpt-x",
            "REDTEAM_LLM_API_KEY_ENV_VAR": "MY_CUSTOM_KEY",
        }
    )
    assert config.api_key_env_var == "MY_CUSTOM_KEY"


def test_is_go_provider_exact_match() -> None:
    assert is_go_provider("opencode-go")
    assert not is_go_provider("litellm")
    assert not is_go_provider("")
    assert not is_go_provider(None)


# --- model-shape validator (single interim grammar; T1 parser supersedes) ---


def test_opencode_go_missing_model_is_invalid() -> None:
    with pytest.raises(ValueError, match="REDTEAM_LLM_MODEL is not set"):
        validate_model("")
    with pytest.raises(ValueError, match="REDTEAM_LLM_MODEL is not set"):
        validate_model("   ")


@pytest.mark.parametrize("model", ["gpt-6-luna", "openai", "anthropic", "foo/bar", "http://x"])
def test_opencode_go_bad_prefix_shape_is_invalid(model: str) -> None:
    with pytest.raises(ValueError, match="invalid REDTEAM_LLM_MODEL"):
        validate_model(model)


def test_opencode_go_empty_responses_suffix_is_invalid() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        validate_model("openai/responses/")


def test_opencode_go_whitespace_model_id_is_invalid() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        validate_model("openai/   ")
    with pytest.raises(ValueError, match="must not be empty"):
        validate_model("anthropic/")


def test_opencode_go_longest_prefix_wins_no_fallback() -> None:
    # "openai/responses/" must NOT be accepted via the shorter "openai/"
    # branch (which would misread the remainder as "responses/").
    with pytest.raises(ValueError, match="must not be empty"):
        validate_model("openai/responses/")
    # A real id under the long prefix returns intact.
    assert validate_model("openai/responses/gpt-x") == "openai/responses/gpt-x"


# --- bootstrap selection ---


def test_bootstrap_selects_go_factory_without_touching_litellm_path(
    monkeypatch: pytest.MonkeyPatch, _isolated_env: None
) -> None:
    sentinel = object()
    go_calls = _stub_go_adapter(monkeypatch, sentinel)
    litellm_calls = _stub_litellm(monkeypatch)
    os.environ["OPENCODE_API_KEY"] = "sk-test"
    os.environ["LLM_API_KEY"] = "sk-litellm"
    go_config = LLMConfig(
        provider="opencode-go",
        default_model="openai/gpt-x",
        api_key_env_var="OPENCODE_API_KEY",
    )
    assert build_llm_client(go_config) is sentinel
    assert go_calls["config"] is go_config
    # The pre-existing litellm branch still builds with (model, api_key).
    build_llm_client(LLMConfig(provider="litellm", default_model="gpt-4o-mini"))
    assert litellm_calls["model"] == "gpt-4o-mini"
    assert litellm_calls["api_key"] == "sk-litellm"


def test_bootstrap_go_bad_model_fails_before_factory(
    monkeypatch: pytest.MonkeyPatch, _isolated_env: None
) -> None:
    go_calls = _stub_go_adapter(monkeypatch, object())
    config = LLMConfig(provider="opencode-go", default_model="gpt-6-luna")
    with pytest.raises(ValueError, match="invalid REDTEAM_LLM_MODEL"):
        build_llm_client(config)
    assert not go_calls


def test_bootstrap_unknown_provider_still_errors(_isolated_env: None) -> None:
    config = LLMConfig(provider="watson", default_model="some-model")
    with pytest.raises(ValueError, match="opencode-go"):
        build_llm_client(config)


# --- onboarding ---


async def test_onboarding_default_empty_choice_stays_litellm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _isolated_env: None
) -> None:
    env_file = _onboarding(monkeypatch, tmp_path)
    llm_config, _, _, _ = await interactive._ensure_runtime_config(
        _ScriptedConsole(["", "gpt-4o-mini", ""])
    )
    assert llm_config.provider == "litellm"
    assert llm_config.default_model == "gpt-4o-mini"
    assert llm_config.api_key_env_var == "LLM_API_KEY"
    content = env_file.read_text(encoding="utf-8")
    assert "REDTEAM_LLM_PROVIDER=litellm" in content
    assert "REDTEAM_LLM_MODEL=gpt-4o-mini" in content
    assert "REDTEAM_LLM_API_KEY_ENV_VAR" not in content


async def test_onboarding_go_writes_opencode_api_key_reference(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _isolated_env: None
) -> None:
    env_file = _onboarding(monkeypatch, tmp_path)
    llm_config, _, _, _ = await interactive._ensure_runtime_config(
        _ScriptedConsole(["opencode-go", "openai/gpt-test-1", ""])
    )
    assert llm_config.provider == "opencode-go"
    assert llm_config.api_key_env_var == OPENCODE_GO_API_KEY_ENV_VAR == "OPENCODE_API_KEY"
    content = env_file.read_text(encoding="utf-8")
    assert "REDTEAM_LLM_PROVIDER=opencode-go" in content
    assert "REDTEAM_LLM_MODEL=openai/gpt-test-1" in content
    assert "REDTEAM_LLM_API_KEY_ENV_VAR=OPENCODE_API_KEY" in content
    assert "OPENCODE_API_KEY=sk-test-key" in content


async def test_onboarding_preserves_existing_api_key_env_var(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _isolated_env: None
) -> None:
    os.environ["REDTEAM_LLM_API_KEY_ENV_VAR"] = "MY_CUSTOM_KEY"
    env_file = _onboarding(monkeypatch, tmp_path)
    llm_config, _, _, _ = await interactive._ensure_runtime_config(
        _ScriptedConsole(["opencode-go", "anthropic/claude-test", ""])
    )
    assert llm_config.api_key_env_var == "MY_CUSTOM_KEY"
    content = env_file.read_text(encoding="utf-8")
    assert "REDTEAM_LLM_API_KEY_ENV_VAR=MY_CUSTOM_KEY" in content
    assert "OPENCODE_API_KEY" not in content
    assert "MY_CUSTOM_KEY=sk-test-key" in content


async def test_onboarding_go_rejects_bad_shape_then_accepts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _isolated_env: None
) -> None:
    _onboarding(monkeypatch, tmp_path)
    console = _ScriptedConsole(["opencode-go", "gpt-6-luna", "openai/responses/gpt-ok", ""])
    llm_config, _, _, _ = await interactive._ensure_runtime_config(console)
    assert llm_config.default_model == "openai/responses/gpt-ok"
    assert "invalid REDTEAM_LLM_MODEL" in " ".join(console.transcript)


async def test_onboarding_unknown_provider_reprompts_to_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _isolated_env: None
) -> None:
    _onboarding(monkeypatch, tmp_path)
    console = _ScriptedConsole(["watson", "", "gpt-4o-mini", ""])
    llm_config, _, _, _ = await interactive._ensure_runtime_config(console)
    assert llm_config.provider == "litellm"
    assert "Choose 'litellm'" in " ".join(console.transcript)


# --- offline validate ---


async def test_validate_reports_go_config_without_provider_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _isolated_env: None, capsys
) -> None:
    os.environ["REDTEAM_LLM_PROVIDER"] = "opencode-go"
    os.environ["REDTEAM_LLM_MODEL"] = "openai/go-test-1"
    _poison_go_adapter(monkeypatch)
    exit_code = await validate_command.run(_validate_args(_engagement(tmp_path)))
    assert exit_code == EXIT_OK


async def test_validate_missing_go_key_value_is_warning_not_error(
    tmp_path: Path, _isolated_env: None, capsys
) -> None:
    os.environ["REDTEAM_LLM_PROVIDER"] = "opencode-go"
    os.environ["REDTEAM_LLM_MODEL"] = "anthropic/cl-test"
    os.environ["REDTEAM_LLM_API_KEY_ENV_VAR"] = "MY_CUSTOM_KEY"
    exit_code = await validate_command.run(_validate_args(_engagement(tmp_path)))
    assert exit_code == EXIT_OK
    assert "API key value not set here" in capsys.readouterr().out


async def test_validate_bad_go_model_fails_with_actionable_error(
    tmp_path: Path, _isolated_env: None, capsys
) -> None:
    os.environ["REDTEAM_LLM_PROVIDER"] = "opencode-go"
    os.environ["REDTEAM_LLM_MODEL"] = "not-a-qualified-model"
    exit_code = await validate_command.run(_validate_args(_engagement(tmp_path)))
    assert exit_code == EXIT_CONFIG_ERROR
    assert "opencode-go" in capsys.readouterr().err


async def test_validate_go_missing_model_fails_with_actionable_error(
    tmp_path: Path, _isolated_env: None, capsys
) -> None:
    os.environ["REDTEAM_LLM_PROVIDER"] = "opencode-go"
    exit_code = await validate_command.run(_validate_args(_engagement(tmp_path)))
    assert exit_code == EXIT_CONFIG_ERROR
    assert "REDTEAM_LLM_MODEL" in capsys.readouterr().err


async def test_validate_unset_llm_still_passes(tmp_path: Path, _isolated_env: None, capsys) -> None:
    exit_code = await validate_command.run(_validate_args(_engagement(tmp_path)))
    assert exit_code == EXIT_OK
    assert "LLM configuration (not configured)" in capsys.readouterr().out


async def test_validate_litellm_default_model_unchanged(
    tmp_path: Path, _isolated_env: None, capsys
) -> None:
    os.environ["REDTEAM_LLM_PROVIDER"] = "litellm"
    exit_code = await validate_command.run(_validate_args(_engagement(tmp_path)))
    assert exit_code == EXIT_OK
    assert "REDTEAM_LLM_MODEL is not set" not in capsys.readouterr().err
