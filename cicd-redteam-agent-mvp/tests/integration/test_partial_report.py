import json
from pathlib import Path

import pytest

from cicd_redteam_agent.application.analyze import AnalysisConfig, analyze_repository
from cicd_redteam_agent.domain.models import (
    AgentWorkingMemory,
    ReportStatus,
    WorkingHypothesis,
    WorkingHypothesisStatus,
)
from cicd_redteam_agent.framework.hackingbuddy_adapter import AgentRunResult

FIXTURES = Path(__file__).parents[1] / "fixtures"


@pytest.mark.asyncio
async def test_reasoning_mode_does_not_prepopulate_deterministic_conclusions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def fake_run_agent_analysis(*args, **_kwargs) -> AgentRunResult:
        state = args[0]
        state.working_memory = AgentWorkingMemory(
            revision=1,
            focus_hypothesis_id="hyp_0123456789abcdef",
            hypotheses=[
                WorkingHypothesis(
                    hypothesis_id="hyp_0123456789abcdef",
                    claim="Artifact provenance may reach privileged execution",
                    status=WorkingHypothesisStatus.BLOCKED,
                    missing_evidence=["runtime artifact provenance"],
                    decision_summary="Local facts do not establish the runtime producer.",
                    created_revision=1,
                    updated_revision=1,
                )
            ],
        )
        return AgentRunResult(
            status=ReportStatus.PARTIAL,
            reason="max_cost",
            rounds=2,
            tokens=20,
            cost=0.5,
            warnings=["Fake budget exhausted."],
            runtime_metrics={"model_attempts": 2, "retries": 1},
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
    assert report.state.findings == []
    assert report.state.paths == []
    assert report.state.identities
    assert persisted["state"]["findings"] == []
    assert persisted["state"]["paths"] == []
    assert persisted["run_metadata"]["analysis_mode"] == "reasoning"
    assert persisted["state"]["termination"]["cost"] == 0.5
    assert persisted["run_metadata"]["agent_runtime"]["model_attempts"] == 2
    assert persisted["run_metadata"]["agent_runtime"]["retries"] == 1
    assert persisted["state"]["working_memory"]["revision"] == 1
    summary = (run_dir / "summary.md").read_text(encoding="utf-8")
    assert "Model attempts: 2" in summary
    assert "Model retries: 1" in summary
    assert "Active investigation state" in summary
    assert "Artifact provenance may reach privileged execution" in summary
