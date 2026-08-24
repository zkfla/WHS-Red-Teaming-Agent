from cicd_redteam_agent.domain.models import (
    AnalysisProposal,
    AttackPath,
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
