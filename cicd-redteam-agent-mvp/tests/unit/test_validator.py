from cicd_redteam_agent.domain.models import (
    AnalysisProposal,
    AttackEdge,
    AttackPath,
    Confidence,
    Finding,
    FindingStatus,
)
from cicd_redteam_agent.domain.validator import validate_analysis_proposal
from tests.unit.test_domain import build_state


def test_unknown_evidence_and_confirmed_path_are_rejected() -> None:
    state = build_state()
    proposal = AnalysisProposal(
        paths=[
            AttackPath(
                path_id="path_bad",
                nodes=[state.repository.repository_id],
                edges=[],
                status=FindingStatus.CONFIRMED,
                evidence_ids=["ev_invented"],
            )
        ]
    )
    result = validate_analysis_proposal(state, proposal)
    assert not result.accepted
    assert any("unknown evidence" in error for error in result.errors)
    assert any("forbids CONFIRMED" in error for error in result.errors)


def test_unordered_edges_and_existing_ids_are_rejected() -> None:
    state = build_state()
    evidence_id = next(iter(state.evidence))
    workflow = state.workflows[0]
    job = workflow.jobs[0]
    step = job.steps[0]
    existing = Finding(
        finding_id="finding_existing",
        rule_id="EXISTING",
        title="Existing deterministic result",
        status=FindingStatus.CANDIDATE,
        evidence_ids=[evidence_id],
        confidence=Confidence.LOW,
        summary="Existing state must not be overwritten by a proposal.",
    )
    state.findings = [existing]
    proposal = AnalysisProposal(
        findings=[existing.model_copy()],
        paths=[
            AttackPath(
                path_id="path_unordered",
                nodes=[workflow.workflow_id, job.job_id, step.step_id],
                edges=[
                    AttackEdge(
                        source_id=job.job_id,
                        relation_type="contains_step",
                        target_id=step.step_id,
                        evidence_ids=[evidence_id],
                    ),
                    AttackEdge(
                        source_id=workflow.workflow_id,
                        relation_type="contains_job",
                        target_id=job.job_id,
                        evidence_ids=[evidence_id],
                    ),
                ],
                status=FindingStatus.CANDIDATE,
                evidence_ids=[evidence_id],
            )
        ],
    )

    result = validate_analysis_proposal(state, proposal)

    assert not result.accepted
    assert any("ordered sequence" in error for error in result.errors)
    assert any("Finding IDs already exist" in error for error in result.errors)
