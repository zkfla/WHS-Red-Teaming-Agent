from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from cicd_redteam_agent.application.natural_cli import (
    is_github_repository_url,
    materialize_target,
    parse_natural_request,
)
from cicd_redteam_agent.application.objective import compile_objective_goal
from cicd_redteam_agent.cli import main
from cicd_redteam_agent.domain.models import AnalysisMode, AttackerProfile
from cicd_redteam_agent.framework.auth import ModelProvider


def test_natural_request_extracts_github_target_and_defaults_to_reasoning() -> None:
    request = parse_natural_request(
        "https://github.com/step-security/github-actions-goat "
        "이 저장소 CI/CD 취약점과 third-party action 공급망 공격 경로를 분석해줘"
    )

    assert request.target == "https://github.com/step-security/github-actions-goat"
    assert request.mode is AnalysisMode.REASONING
    assert request.provider is ModelProvider.CODEX_CHATGPT
    assert request.attacker_profile is AttackerProfile.FORK_PR
    assert request.objective is not None
    goal = compile_objective_goal(request.objective)
    assert "mutable_dependency" in goal.mechanism_hints


def test_natural_request_extracts_model_and_removes_it_from_objective() -> None:
    request = parse_natural_request(
        "https://github.com/step-security/github-actions-goat "
        "gpt-5.6-luna로 CI/CD 취약점을 깊게 분석해줘"
    )

    assert request.model == "gpt-5.6-luna"
    assert request.mode is AnalysisMode.REASONING
    assert request.provider is ModelProvider.CODEX_CHATGPT
    assert request.objective == "CI/CD 취약점을 깊게 분석해줘"


def test_natural_request_default_model_phrase_keeps_codex_default() -> None:
    request = parse_natural_request(
        "https://github.com/step-security/github-actions-goat 기본 모델로 분석해줘"
    )

    assert request.model is None
    assert request.objective == "분석해줘"


def test_natural_request_ignores_model_when_deterministic(tmp_path: Path) -> None:
    request = parse_natural_request(
        "gpt-5.6-sol로 AI 없이 정적 분석만 해줘",
        explicit_target=str(tmp_path),
    )

    assert request.mode is AnalysisMode.DETERMINISTIC
    assert request.model is None
    assert request.objective is None


def test_natural_request_extracts_existing_local_path_prefix(tmp_path: Path) -> None:
    request = parse_natural_request(f"{tmp_path} 이 저장소 취약점 분석해줘")

    assert request.target == str(tmp_path)
    assert request.objective == "이 저장소 취약점 분석해줘"


def test_natural_request_understands_deterministic_and_repo_writer_intent(tmp_path: Path) -> None:
    request = parse_natural_request(
        "AI 없이 정적 분석만 하고 쓰기 권한 가진 공격자 관점으로 봐줘",
        explicit_target=str(tmp_path),
    )

    assert request.target == str(tmp_path)
    assert request.mode is AnalysisMode.DETERMINISTIC
    assert request.provider is ModelProvider.AUTO
    assert request.attacker_profile is AttackerProfile.REPO_WRITER
    assert request.objective is None


def test_natural_request_requires_explicit_words_for_live_observation(tmp_path: Path) -> None:
    ordinary = parse_natural_request(
        "GitHub Actions 취약점 분석해줘", explicit_target=str(tmp_path)
    )
    observed = parse_natural_request(
        "실제 GitHub 설정과 실제 권한까지 GitHub API로 확인해줘 run id 4242",
        explicit_target=str(tmp_path),
    )

    assert not ordinary.observe_github
    assert observed.observe_github
    assert observed.github_run_id == 4242


def test_materialize_target_returns_existing_local_directory(tmp_path: Path) -> None:
    assert materialize_target(str(tmp_path), cache_root=tmp_path / "cache") == tmp_path.resolve()


def test_materialize_target_clones_strict_github_url(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        Path(command[-1]).mkdir(parents=True)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("cicd_redteam_agent.application.natural_cli.subprocess.run", fake_run)
    url = "https://github.com/step-security/github-actions-goat"
    target = materialize_target(url, cache_root=tmp_path / "cache")

    assert target.is_dir()
    assert calls
    assert calls[0][:4] == ["git", "clone", "--depth", "1"]
    assert calls[0][-2] == url
    assert is_github_repository_url(url)
    assert not is_github_repository_url("https://example.com/org/repo")


def test_natural_request_without_target_fails_closed() -> None:
    with pytest.raises(ValueError, match="분석 대상 저장소"):
        parse_natural_request("이 저장소 취약점 분석해줘")


def test_cli_without_arguments_enters_natural_mode(monkeypatch) -> None:
    captured: list[str | None] = []

    def fake_natural(request: str | None = None) -> int:
        captured.append(request)
        return 0

    monkeypatch.setattr("cicd_redteam_agent.cli._natural_analysis", fake_natural)

    assert main([]) == 0
    assert captured == [None]


def test_cli_routes_unknown_first_argument_to_natural_mode(monkeypatch) -> None:
    captured: list[str | None] = []

    def fake_natural(request: str | None = None) -> int:
        captured.append(request)
        return 0

    monkeypatch.setattr("cicd_redteam_agent.cli._natural_analysis", fake_natural)
    code = main(
        [
            "https://github.com/step-security/github-actions-goat",
            "이 저장소 CI/CD 취약점 분석해줘",
        ]
    )

    assert code == 0
    assert captured == [
        "https://github.com/step-security/github-actions-goat 이 저장소 CI/CD 취약점 분석해줘"
    ]
