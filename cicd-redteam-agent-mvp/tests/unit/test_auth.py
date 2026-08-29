from pathlib import Path

import pytest

from cicd_redteam_agent.framework.auth import (
    PROVIDER_KEYS,
    ModelProvider,
    resolve_github_observer_token,
    resolve_litellm_credential,
)


def _clear_provider_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in PROVIDER_KEYS.values():
        monkeypatch.delenv(variable, raising=False)


def _clear_github_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


def test_env_file_is_loaded_and_explicit_provider_selects_only_its_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_provider_keys(monkeypatch)
    target = tmp_path / "target"
    target.mkdir()
    env_file = tmp_path / "operator.env"
    env_file.write_text(
        "OPENAI_API_KEY=openai-test\nANTHROPIC_API_KEY=anthropic-test\n",
        encoding="utf-8",
    )

    credential = resolve_litellm_credential(
        ModelProvider.ANTHROPIC,
        "anthropic/claude-test",
        env_file=env_file,
        untrusted_repository_root=target,
    )

    assert credential.provider is ModelProvider.ANTHROPIC
    assert credential.api_key == "anthropic-test"
    assert credential.env_file == env_file.resolve()


def test_project_env_is_discovered_automatically_and_process_value_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_provider_keys(monkeypatch)
    operator_root = tmp_path / "operator"
    target = operator_root / "target"
    target.mkdir(parents=True)
    (operator_root / ".env").write_text("OPENAI_API_KEY=dotenv-test\n", encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "process-test")
    monkeypatch.chdir(operator_root)

    credential = resolve_litellm_credential(
        ModelProvider.OPENAI,
        "openai/gpt-test",
        env_file=None,
        untrusted_repository_root=target,
    )

    assert credential.api_key == "process-test"
    assert credential.env_file == (operator_root / ".env").resolve()


def test_auto_requires_selection_when_multiple_keys_exist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_provider_keys(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "openai-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-test")

    with pytest.raises(ValueError, match="choose one with --provider"):
        resolve_litellm_credential(
            ModelProvider.AUTO,
            "openai/gpt-test",
            env_file=None,
            untrusted_repository_root=tmp_path,
        )


def test_model_provider_mismatch_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_provider_keys(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-test")

    with pytest.raises(ValueError, match="identifies provider 'openai'"):
        resolve_litellm_credential(
            ModelProvider.ANTHROPIC,
            "openai/gpt-test",
            env_file=None,
            untrusted_repository_root=tmp_path,
        )


def test_github_observer_prefers_gh_token_process_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_github_keys(monkeypatch)
    target = tmp_path / "target"
    target.mkdir()
    monkeypatch.setenv("GH_TOKEN", "gh-preferred")
    monkeypatch.setenv("GITHUB_TOKEN", "github-fallback")

    token = resolve_github_observer_token(
        None,
        untrusted_repository_root=target,
    )

    assert token == "gh-preferred"


def test_github_observer_loads_only_trusted_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_github_keys(monkeypatch)
    target = tmp_path / "target"
    target.mkdir()
    env_file = tmp_path / "operator.env"
    env_file.write_text("GITHUB_TOKEN=trusted-github-token\n", encoding="utf-8")

    token = resolve_github_observer_token(
        env_file,
        untrusted_repository_root=target,
    )

    assert token == "trusted-github-token"


def test_github_observer_rejects_env_inside_analyzed_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_github_keys(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text("GH_TOKEN=untrusted\n", encoding="utf-8")

    with pytest.raises(ValueError, match="outside the analyzed repository"):
        resolve_github_observer_token(
            env_file,
            untrusted_repository_root=tmp_path,
        )


def test_env_file_inside_analyzed_repository_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_provider_keys(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=untrusted\n", encoding="utf-8")

    with pytest.raises(ValueError, match="outside the analyzed repository"):
        resolve_litellm_credential(
            ModelProvider.OPENAI,
            "openai/gpt-test",
            env_file=env_file,
            untrusted_repository_root=tmp_path,
        )
