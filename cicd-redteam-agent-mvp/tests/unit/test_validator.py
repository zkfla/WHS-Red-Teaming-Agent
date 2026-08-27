from cicd_redteam_agent.domain.models import (
    AnalysisProposal,
    AttackEdge,
    AttackPath,
    Confidence,
    Finding,
    FindingStatus,
    Relation,
    TriState,
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


def test_false_relation_requires_rejected_status_and_complete_edge_evidence() -> None:
    state = build_state()
    evidence_ids = list(state.evidence)[:2]
    relation = Relation(
        source_id="input:test",
        relation_type="guarded_flow",
        target_id="step:test",
        category="execution",
        verdict=TriState.FALSE,
        evidence_ids=evidence_ids,
    )
    state.relations.append(relation)
    candidate = AnalysisProposal(
        paths=[
            AttackPath(
                path_id="path_false_candidate",
                nodes=[relation.source_id, relation.target_id],
                edges=[
                    AttackEdge(
                        relation_id=relation.relation_id,
                        source_id=relation.source_id,
                        relation_type=relation.relation_type,
                        target_id=relation.target_id,
                        evidence_ids=relation.evidence_ids,
                    )
                ],
                status=FindingStatus.CANDIDATE,
                evidence_ids=relation.evidence_ids[:1],
            )
        ]
    )
    result = validate_analysis_proposal(state, candidate)
    assert not result.accepted
    assert any("FALSE relation must be REJECTED" in error for error in result.errors)
    assert any("omits edge evidence" in error for error in result.errors)

    rejected = candidate.model_copy(deep=True)
    rejected.paths[0].path_id = "path_false_rejected"
    rejected.paths[0].status = FindingStatus.REJECTED
    rejected.paths[0].evidence_ids = relation.evidence_ids
    assert validate_analysis_proposal(state, rejected).accepted
