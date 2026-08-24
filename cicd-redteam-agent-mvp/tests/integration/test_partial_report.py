import json
from pathlib import Path

import pytest

from cicd_redteam_agent.application.analyze import AnalysisConfig, analyze_repository
from cicd_redteam_agent.domain.models import ReportStatus
from cicd_redteam_agent.framework.hackingbuddy_adapter import AgentRunResult

FIXTURES = Path(__file__).parents[1] / "fixtures"


@pytest.mark.asyncio
async def test_partial_agent_result_preserves_deterministic_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def fake_run_agent_analysis(*_args, **_kwargs) -> AgentRunResult:
        return AgentRunResult(
            status=ReportStatus.PARTIAL,
            reason="max_cost",
            rounds=2,
            tokens=20,
            cost=0.5,
            warnings=["Fake budget exhausted."],
        )

    monkeypatch.setattr(
        "cicd_redteam_agent.framework.hackingbuddy_adapter.run_agent_analysis",
        fake_run_agent_analysis,
    )

    report, run_dir = await analyze_repository(
        FIXTURES / "oidc-risk",
        AnalysisConfig(output_dir=tmp_path, model="fake/local"),
    )

    persisted = json.loads((run_dir / "analysis.json").read_text(encoding="utf-8"))
    assert report.status is ReportStatus.PARTIAL
    assert report.termination_reason == "max_cost"
    assert report.state.findings
    assert report.state.identities
    assert persisted["state"]["findings"]
    assert persisted["state"]["termination"]["cost"] == 0.5
