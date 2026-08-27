from pathlib import Path

import pytest

from cicd_redteam_agent.application.analyze import AnalysisConfig, analyze_repository
from cicd_redteam_agent.domain.models import (
    AnalysisProposal,
    AttackEdge,
    AttackPath,
    FindingStatus,
)
from cicd_redteam_agent.domain.reasoning_evaluation import (
    ReasoningExpectation,
    evaluate_reasoning_report,
)
from cicd_redteam_agent.domain.validator import apply_analysis_proposal

FIXTURE = Path(__file__).parents[1] / "fixtures" / "blind-composite"


def _next(relations, source: str, relation_type: str):
    return next(
        item
        for item in relations
        if item.source_id == source and item.relation_type == relation_type
    )


@pytest.mark.asyncio
async def test_blind_composite_has_two_grounded_cross_workflow_paths(
    tmp_path: Path,
) -> None:
    report, _ = await analyze_repository(
        FIXTURE, AnalysisConfig(output_dir=tmp_path / "blind")
    )
    relations = report.state.relations

    source = next(
        item
        for item in relations
        if item.relation_type == "controls_repository_file"
        and item.target_id == "file:actions/package/action.yml"
    )
    local_execution = _next(
        relations, source.target_id, "repository_file_executes_in"
    )
    artifact = _next(
        relations,
        local_execution.target_id,
        "repository_execution_may_produce_artifact",
    )
    artifact_execution = _next(
        relations, artifact.target_id, "artifact_content_reaches_execution"
    )
    token = _next(
        relations,
        artifact_execution.target_id,
        "can_use_github_token_contents_write",
    )
    supported_chain = [source, local_execution, artifact, artifact_execution, token]

    oidc = _next(relations, artifact_execution.target_id, "can_request_oidc_token")
    cloud = _next(relations, oidc.target_id, "oidc_may_assume_cloud_identity")
    candidate_chain = [*supported_chain[:-1], oidc, cloud]

    paths = []
    for path_id, chain, status, missing in (
        ("path_blind_token", supported_chain, FindingStatus.SUPPORTED, []),
        (
            "path_blind_cloud",
            candidate_chain,
            FindingStatus.CANDIDATE,
            cloud.missing_evidence,
        ),
    ):
        paths.append(
            AttackPath(
                path_id=path_id,
                nodes=[chain[0].source_id, *(item.target_id for item in chain)],
                edges=[
                    AttackEdge(
                        relation_id=item.relation_id,
                        source_id=item.source_id,
                        relation_type=item.relation_type,
                        target_id=item.target_id,
                        evidence_ids=item.evidence_ids,
                    )
                    for item in chain
                ],
                status=status,
                evidence_ids=sorted(
                    {evidence for item in chain for evidence in item.evidence_ids}
                ),
                missing_evidence=missing,
            )
        )
    proposal = AnalysisProposal(paths=paths)
    result = apply_analysis_proposal(report.state, proposal)
    assert result.accepted, result.errors

    evaluation = evaluate_reasoning_report(
        report,
        ReasoningExpectation(
            scenario_ids=("blind-composite",),
            variant="blind",
            expected_status=FindingStatus.SUPPORTED,
            required_relation_types=frozenset(
                {
                    "controls_repository_file",
                    "repository_execution_may_produce_artifact",
                    "artifact_content_reaches_execution",
                    "can_use_github_token_contents_write",
                }
            ),
            path_relation_types=frozenset(
                {
                    "repository_execution_may_produce_artifact",
                    "artifact_content_reaches_execution",
                }
            ),
        ),
    )
    assert evaluation.passed

    relation_types = {item.relation_type for item in relations}
    assert "repository_execution_may_populate_cache" in relation_types
    assert "cache_producer_reaches_privileged_consumer" in relation_types
