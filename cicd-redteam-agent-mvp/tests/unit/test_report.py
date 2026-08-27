from datetime import UTC, datetime

from cicd_redteam_agent.application.report import render_summary
from cicd_redteam_agent.domain.models import (
    AnalysisReport,
    AnalysisState,
    AttackEdge,
    AttackPath,
    Confidence,
    Evidence,
    Finding,
    FindingStatus,
    ReportStatus,
    RepositorySnapshot,
    TraceMetadata,
)


def _report() -> AnalysisReport:
    evidence = Evidence(
        evidence_id="ev_1",
        kind="workflow-field",
        source_path=".github/workflows/ci.yml",
        line_start=4,
        line_end=5,
        field_path="jobs.test.permissions",
        redacted_excerpt="permissions: write-all",
        content_hash="abc123",
    )
    state = AnalysisState(
        repository=RepositorySnapshot(
            repository_id="repo_test",
            root="C:/repository",
            git_commit="abc123",
            dirty=False,
            analyzed_at=datetime.now(UTC),
        ),
        evidence={evidence.evidence_id: evidence},
        findings=[
            Finding(
                finding_id="finding_1",
                rule_id="TEST",
                title="Untrusted | heading # injection",
                status=FindingStatus.CANDIDATE,
                evidence_ids=[evidence.evidence_id],
                missing_evidence=["repository default permission"],
                confidence=Confidence.MEDIUM,
                summary="Candidate *summary* from repository text",
            )
        ],
        paths=[
            AttackPath(
                path_id="path_1",
                nodes=[
                    "attacker:fork-pr",
                    "step:test",
                    "credential:github-token:job:test",
                ],
                edges=[
                    AttackEdge(
                        source_id="attacker:fork-pr",
                        relation_type="untrusted_input_reaches_execution",
                        target_id="step:test",
                        evidence_ids=[evidence.evidence_id],
                    ),
                    AttackEdge(
                        source_id="step:test",
                        relation_type="can_access_write_github_token",
                        target_id="credential:github-token:job:test",
                        evidence_ids=[evidence.evidence_id],
                    ),
                ],
                status=FindingStatus.CANDIDATE,
                evidence_ids=[evidence.evidence_id],
                missing_evidence=["repository default permission"],
            )
        ],
    )
    return AnalysisReport(
        status=ReportStatus.COMPLETE,
        termination_reason="deterministic_analysis_complete",
        state=state,
        warnings=["warning", "warning"],
        trace=TraceMetadata(framework_commit="test"),
        run_metadata={"analysis_mode": "deterministic", "provider": "auto"},
    )


def test_summary_surfaces_coverage_and_evidence_locations() -> None:
    summary = render_summary(_report())

    assert "## At a glance" in summary
    assert "## Analysis coverage" in summary
    assert "## Interpretation" in summary
    assert "Review priority: **1 of 1**" in summary
    assert "Recommended actions:" in summary
    assert "Reduce GITHUB\\_TOKEN permissions" in summary
    assert "`.github/workflows/ci.yml`" not in summary
    assert "ev_1` (.github/workflows/ci.yml:4-5)" in summary


def test_summary_escapes_untrusted_markdown_and_deduplicates_warnings() -> None:
    summary = render_summary(_report())

    assert "Untrusted \\| heading \\# injection" in summary
    assert "Candidate \\*summary\\* from repository text" in summary
    assert summary.count("- warning") == 1
