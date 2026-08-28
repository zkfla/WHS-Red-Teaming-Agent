import io
import json
from pathlib import Path

import pytest

from cicd_redteam_agent.cli import build_parser, main
from cicd_redteam_agent.domain.models import ReportStatus
from cicd_redteam_agent.framework.hackingbuddy_adapter import AgentRunResult

FIXTURES = Path(__file__).parents[1] / "fixtures"


class InteractiveInput(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_invalid_input_and_limits_return_exit_2(tmp_path: Path) -> None:
    assert main(["analyze", str(tmp_path / "missing"), "--output", str(tmp_path)]) == 2
    assert (
        main(
            [
                "analyze",
                str(FIXTURES / "oidc-risk"),
                "--output",
                str(tmp_path),
                "--max-rounds",
                "0",
            ]
        )
        == 2
    )
    assert (
        main(
            [
                "analyze",
                str(FIXTURES / "oidc-risk"),
                "--output",
                str(tmp_path),
                "--provider",
                "codex-chatgpt",
                "--env-file",
                str(tmp_path / ".env"),
            ]
        )
        == 2
    )


def test_deterministic_failure_writes_failed_report_and_returns_exit_5(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fail_parse(*_args, **_kwargs):
        raise RuntimeError("token=should-not-appear")

    monkeypatch.setattr("cicd_redteam_agent.application.analyze.parse_workflow", fail_parse)
    output = tmp_path / "output"

    exit_code = main(
        ["analyze", str(FIXTURES / "oidc-risk"), "--output", str(output)]
    )

    run_dir = next(output.iterdir())
    raw = (run_dir / "analysis.json").read_text(encoding="utf-8")
    report = json.loads(raw)
    assert exit_code == 5
    assert report["status"] == "FAILED"
    assert report["termination_reason"] == "deterministic_analysis_failed"
    assert "should-not-appear" not in raw
    assert "[REDACTED]" in raw


def test_framework_initialization_failure_returns_exit_3(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def fail_agent(*_args, **_kwargs):
        raise ModuleNotFoundError("fake provider unavailable")

    monkeypatch.setattr(
        "cicd_redteam_agent.framework.hackingbuddy_adapter.run_agent_analysis",
        fail_agent,
    )

    assert (
        main(
            [
                "analyze",
                str(FIXTURES / "oidc-risk"),
                "--output",
                str(tmp_path / "output"),
                "--model",
                "fake/local",
            ]
        )
        == 3
    )


def test_partial_agent_result_returns_exit_4(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def partial_agent(*_args, **_kwargs) -> AgentRunResult:
        return AgentRunResult(
            status=ReportStatus.PARTIAL,
            reason="max_tokens",
            rounds=2,
            tokens=100,
            cost=0.1,
            warnings=[],
        )

    monkeypatch.setattr(
        "cicd_redteam_agent.framework.hackingbuddy_adapter.run_agent_analysis",
        partial_agent,
    )

    assert (
        main(
            [
                "analyze",
                str(FIXTURES / "oidc-risk"),
                "--output",
                str(tmp_path / "output"),
                "--model",
                "fake/local",
            ]
        )
        == 4
    )


def test_objective_requires_reasoning_mode(tmp_path: Path) -> None:
    assert (
        main(
            [
                "analyze",
                str(FIXTURES / "oidc-risk"),
                "--output",
                str(tmp_path / "output"),
                "--objective",
                "inspect OIDC",
            ]
        )
        == 2
    )


def test_objective_sources_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit) as raised:
        build_parser().parse_args(
            [
                "analyze",
                "repo",
                "--output",
                "output",
                "--objective",
                "one",
                "--objective-file",
                "two.txt",
            ]
        )
    assert raised.value.code == 2


def test_reasoning_objective_reaches_agent_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def complete_agent(_state, _boundary, config, _run_dir, **_kwargs) -> AgentRunResult:
        assert config.objective == "inspect OIDC\nthen cache"
        return AgentRunResult(
            status=ReportStatus.COMPLETE,
            reason="proposal_validated",
            rounds=1,
            tokens=10,
            cost=0.0,
            warnings=[],
            provider="codex-chatgpt",
        )

    monkeypatch.setattr(
        "cicd_redteam_agent.framework.hackingbuddy_adapter.run_agent_analysis",
        complete_agent,
    )

    assert (
        main(
            [
                "analyze",
                str(FIXTURES / "oidc-risk"),
                "--output",
                str(tmp_path / "output"),
                "--provider",
                "codex-chatgpt",
                "--objective",
                "  inspect OIDC\r\nthen cache  ",
            ]
        )
        == 0
    )


def test_reasoning_prompts_for_objective_in_an_interactive_terminal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def complete_agent(_state, _boundary, config, _run_dir, **_kwargs) -> AgentRunResult:
        assert config.objective == "외부 PR에서 Artifact 경로를 조사해줘"
        return AgentRunResult(
            status=ReportStatus.COMPLETE,
            reason="proposal_validated",
            rounds=1,
            tokens=10,
            cost=0.0,
            warnings=[],
            provider="codex-chatgpt",
        )

    monkeypatch.setattr(
        "cicd_redteam_agent.framework.hackingbuddy_adapter.run_agent_analysis",
        complete_agent,
    )

    monkeypatch.setattr(
        "sys.stdin", InteractiveInput("외부 PR에서 Artifact 경로를 조사해줘\n")
    )

    assert (
        main(
            [
                "analyze",
                str(FIXTURES / "oidc-risk"),
                "--output",
                str(tmp_path / "output"),
                "--provider",
                "codex-chatgpt",
            ]
        )
        == 0
    )
    assert "분석 목표를 입력하세요" in capsys.readouterr().out


def test_interactive_objective_prompt_can_be_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def complete_agent(_state, _boundary, config, _run_dir, **_kwargs) -> AgentRunResult:
        assert config.objective is None
        return AgentRunResult(
            status=ReportStatus.COMPLETE,
            reason="proposal_validated",
            rounds=1,
            tokens=10,
            cost=0.0,
            warnings=[],
            provider="codex-chatgpt",
        )

    interactive_input = InteractiveInput("must not be consumed\n")
    monkeypatch.setattr(
        "cicd_redteam_agent.framework.hackingbuddy_adapter.run_agent_analysis",
        complete_agent,
    )
    monkeypatch.setattr("sys.stdin", interactive_input)

    assert (
        main(
            [
                "analyze",
                str(FIXTURES / "oidc-risk"),
                "--output",
                str(tmp_path / "output"),
                "--provider",
                "codex-chatgpt",
                "--no-objective-prompt",
            ]
        )
        == 0
    )
    assert interactive_input.tell() == 0


def test_interactive_terminal_can_select_reasoning_mode_and_objective(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def complete_agent(_state, _boundary, config, _run_dir, **_kwargs) -> AgentRunResult:
        assert config.resolved_mode is not None
        assert config.resolved_mode.value == "reasoning"
        assert config.provider.value == "codex-chatgpt"
        assert config.objective == "Artifact에서 Cloud Identity까지 조사해줘"
        return AgentRunResult(
            status=ReportStatus.COMPLETE,
            reason="proposal_validated",
            rounds=1,
            tokens=10,
            cost=0.0,
            warnings=[],
            provider="codex-chatgpt",
        )

    monkeypatch.setattr(
        "cicd_redteam_agent.framework.hackingbuddy_adapter.run_agent_analysis",
        complete_agent,
    )
    monkeypatch.setattr(
        "sys.stdin",
        InteractiveInput("2\nArtifact에서 Cloud Identity까지 조사해줘\n"),
    )

    assert (
        main(
            [
                "analyze",
                str(FIXTURES / "oidc-risk"),
                "--output",
                str(tmp_path / "output"),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "분석 방식을 선택하세요" in output
    assert "AI 심층 공격 경로 분석 (추천)" in output
    assert "분석 목표를 입력하세요" in output


def test_interactive_terminal_basic_analysis_skips_objective_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("sys.stdin", InteractiveInput("1\n"))

    assert (
        main(
            [
                "analyze",
                str(FIXTURES / "oidc-risk"),
                "--output",
                str(tmp_path / "output"),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "분석 방식을 선택하세요" in output
    assert "분석 목표를 입력하세요" not in output


def test_objective_can_be_read_from_explicit_stdin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def complete_agent(_state, _boundary, config, _run_dir, **_kwargs) -> AgentRunResult:
        assert config.objective == "inspect artifacts"
        return AgentRunResult(
            status=ReportStatus.COMPLETE,
            reason="proposal_validated",
            rounds=1,
            tokens=10,
            cost=0.0,
            warnings=[],
            provider="codex-chatgpt",
        )

    monkeypatch.setattr(
        "cicd_redteam_agent.framework.hackingbuddy_adapter.run_agent_analysis",
        complete_agent,
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("  inspect artifacts\n"))

    assert (
        main(
            [
                "analyze",
                str(FIXTURES / "oidc-risk"),
                "--output",
                str(tmp_path / "output"),
                "--provider",
                "codex-chatgpt",
                "--objective-file",
                "-",
            ]
        )
        == 0
    )


def test_codex_chatgpt_backend_can_run_without_model_argument(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def complete_agent(*_args, **_kwargs) -> AgentRunResult:
        return AgentRunResult(
            status=ReportStatus.COMPLETE,
            reason="proposal_validated",
            rounds=1,
            tokens=10,
            cost=0.0,
            warnings=[],
            provider="codex-chatgpt",
        )

    monkeypatch.setattr(
        "cicd_redteam_agent.framework.hackingbuddy_adapter.run_agent_analysis",
        complete_agent,
    )

    assert (
        main(
            [
                "analyze",
                str(FIXTURES / "oidc-risk"),
                "--output",
                str(tmp_path / "output"),
                "--provider",
                "codex-chatgpt",
            ]
        )
        == 0
    )
