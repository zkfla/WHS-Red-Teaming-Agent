import json
from pathlib import Path

import pytest

from cicd_redteam_agent.cli import main
from cicd_redteam_agent.domain.models import ReportStatus
from cicd_redteam_agent.framework.hackingbuddy_adapter import AgentRunResult

FIXTURES = Path(__file__).parents[1] / "fixtures"


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
