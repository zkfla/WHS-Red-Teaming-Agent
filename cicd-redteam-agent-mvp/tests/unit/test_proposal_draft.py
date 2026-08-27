from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from cicd_redteam_agent.application.state import create_initial_state
from cicd_redteam_agent.domain.models import (
    AnalysisProposalDraft,
    Evidence,
    FindingStatus,
    Relation,
    TriState,
)
from cicd_redteam_agent.domain.proposal_draft import (
    ProposalDraftError,
    expand_analysis_proposal_draft,
)
from cicd_redteam_agent.domain.validator import validate_analysis_proposal
from cicd_redteam_agent.framework.capabilities import CapabilityContext, build_capabilities
from cicd_redteam_agent.security.repository_boundary import RepositoryBoundary


def _state(tmp_path: Path):
    repository = tmp_path / "repository"
    repository.mkdir()
    state = create_initial_state(RepositoryBoundary(repository))
    for index in range(1, 3):
        evidence_id = f"ev_{index}"
        state.evidence[evidence_id] = Evidence(
            evidence_id=evidence_id,
            kind="test",
            source_path=".github/workflows/ci.yml",
            line_start=index,
            line_end=index,
            field_path=f"$.test[{index}]",
            redacted_excerpt=f"test {index}",
            content_hash=str(index) * 64,
        )
    state.relations = [
        Relation(
            relation_id="rel_exact_source",
            source_id="attacker:fork-pr",
            relation_type="controls_repository_file",
            target_id="file:.github/workflows/ci.yml",
            category="source",
            verdict=TriState.TRUE,
            evidence_ids=["ev_1"],
        ),
        Relation(
            relation_id="rel_exact_sink",
            source_id="file:.github/workflows/ci.yml",
            relation_type="may_reach_cloud_identity",
            target_id="cloud-identity:test",
            category="credential",
            verdict=TriState.UNKNOWN,
            evidence_ids=["ev_2"],
            missing_evidence=["cloud trust policy"],
        ),
    ]
    return state, RepositoryBoundary(repository)


def _draft() -> AnalysisProposalDraft:
    return AnalysisProposalDraft.model_validate(
        {
            "path_drafts": [
                {
                    "path_id": "path_compact",
                    "relation_refs": ["R0001", "R0002"],
                }
            ],
            "finding_drafts": [
                {
                    "finding_id": "finding_compact",
                    "rule_id": "COMPACT-DRAFT",
                    "title": "Compact grounded candidate",
                    "path_id": "path_compact",
                    "confidence": "MEDIUM",
                    "summary": "Runtime expands exact graph fields from short references.",
                }
            ],
        }
    )


def test_compact_draft_expands_exact_graph_and_evidence(tmp_path: Path) -> None:
    state, _ = _state(tmp_path)

    proposal = expand_analysis_proposal_draft(state, _draft())

    path = proposal.paths[0]
    assert path.nodes == [
        "attacker:fork-pr",
        "file:.github/workflows/ci.yml",
        "cloud-identity:test",
    ]
    assert [edge.relation_id for edge in path.edges] == [
        "rel_exact_source",
        "rel_exact_sink",
    ]
    assert path.evidence_ids == ["ev_1", "ev_2"]
    assert path.missing_evidence == ["cloud trust policy"]
    assert path.status is FindingStatus.CANDIDATE
    assert proposal.findings[0].evidence_ids == path.evidence_ids
    assert validate_analysis_proposal(state, proposal).accepted


def test_compact_draft_rejects_unknown_or_discontinuous_refs(tmp_path: Path) -> None:
    state, _ = _state(tmp_path)
    unknown = _draft().model_copy(deep=True)
    unknown.path_drafts[0].relation_refs = ["R9999"]
    with pytest.raises(ProposalDraftError, match="unknown relations"):
        expand_analysis_proposal_draft(state, unknown)

    discontinuous = _draft().model_copy(deep=True)
    discontinuous.path_drafts[0].relation_refs = ["R0002", "R0001"]
    with pytest.raises(ProposalDraftError, match="not continuous"):
        expand_analysis_proposal_draft(state, discontinuous)


def test_compact_draft_derives_rejected_and_forbids_status_override(
    tmp_path: Path,
) -> None:
    state, _ = _state(tmp_path)
    state.relations[1].verdict = TriState.FALSE
    state.relations[1].missing_evidence = []

    proposal = expand_analysis_proposal_draft(state, _draft())

    assert proposal.paths[0].status is FindingStatus.REJECTED
    assert proposal.findings[0].status is FindingStatus.REJECTED
    assert proposal.paths[0].missing_evidence == []
    assert validate_analysis_proposal(state, proposal).accepted

    overclaimed = _draft().model_dump(mode="json")
    overclaimed["path_drafts"][0]["status"] = "SUPPORTED"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AnalysisProposalDraft.model_validate(overclaimed)


@pytest.mark.asyncio
async def test_capabilities_publish_refs_and_accept_compact_draft(tmp_path: Path) -> None:
    state, boundary = _state(tmp_path)
    capabilities = build_capabilities(CapabilityContext(state, boundary))

    list_schema = capabilities["ListSecurityFacts"].to_model()
    facts = json.loads(await list_schema.model_validate({}).execute())
    assert [item["relation_ref"] for item in facts["facts"]] == ["R0001", "R0002"]

    submit_schema = capabilities["SubmitAnalysis"].to_model()
    result = json.loads(
        await submit_schema.model_validate(
            {"proposal_json": _draft().model_dump_json()}
        ).execute()
    )

    assert result["accepted"] is True
    assert state.paths[0].edges[1].relation_id == "rel_exact_sink"
    assert state.findings[0].status is FindingStatus.CANDIDATE
