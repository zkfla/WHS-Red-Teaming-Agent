from pathlib import Path

import pytest

from cicd_redteam_agent.application.analyze import AnalysisConfig, analyze_repository
from cicd_redteam_agent.domain.models import (
    AnalysisProposal,
    AttackEdge,
    AttackPath,
    FindingStatus,
    Relation,
    relation_id_for,
)
from cicd_redteam_agent.domain.validator import validate_analysis_proposal
from cicd_redteam_agent.framework.capabilities import CapabilityContext, ListSecurityFacts
from cicd_redteam_agent.security.repository_boundary import RepositoryBoundary

FIXTURES = Path(__file__).parents[1] / "fixtures"


async def _analyze(name: str, variant: str | None, tmp_path: Path):
    repository = FIXTURES / name
    if variant:
        repository /= variant
    report, _ = await analyze_repository(
        repository, AnalysisConfig(output_dir=tmp_path / "output")
    )
    return report.state


@pytest.mark.asyncio
async def test_supported_env_output_path_is_extracted_without_a_scenario_rule(
    tmp_path: Path,
) -> None:
    state = await _analyze("inference-flow", "supported", tmp_path)
    relation_types = {item.relation_type for item in state.relations}

    assert "value_flows_to_env" in relation_types
    assert "env_value_flows_to_step_output" in relation_types
    assert "value_flows_to_step_output" not in relation_types
    assert "output_value_interpreted_as_command" not in relation_types
    assert "env_value_interpreted_as_command" in relation_types
    assert "can_use_github_token_contents_write" in relation_types

    source = next(
        item
        for item in state.relations
        if item.relation_type == "controls_workflow_input"
    )
    assert source.relation_id == relation_id_for(
        source.source_id, source.relation_type, source.target_id
    )
    duplicate = Relation(
        source_id=source.source_id,
        relation_type=source.relation_type,
        target_id=source.target_id,
        category=source.category,
        evidence_ids=source.evidence_ids,
    )
    assert duplicate.relation_id == source.relation_id
    first_env = next(
        item
        for item in state.relations
        if item.relation_type == "value_flows_to_env" and item.source_id == source.target_id
    )
    output = next(
        item
        for item in state.relations
        if item.relation_type == "env_value_flows_to_step_output"
        and item.source_id == first_env.target_id
    )
    second_env = next(
        item
        for item in state.relations
        if item.relation_type == "value_flows_to_env" and item.source_id == output.target_id
    )
    execution = next(
        item
        for item in state.relations
        if item.relation_type == "env_value_interpreted_as_command"
        and item.source_id == second_env.target_id
    )
    credential = next(
        item
        for item in state.relations
        if item.relation_type == "can_use_github_token_contents_write"
        and item.source_id == execution.target_id
    )
    chain = [source, first_env, output, second_env, execution, credential]
    nodes = [chain[0].source_id, *(item.target_id for item in chain)]
    proposal = AnalysisProposal(
        paths=[
            AttackPath(
                path_id="path_llm_supported",
                nodes=nodes,
                edges=[
                    AttackEdge(
                        source_id=item.source_id,
                        relation_type=item.relation_type,
                        target_id=item.target_id,
                        evidence_ids=item.evidence_ids,
                    )
                    for item in chain
                ],
                status=FindingStatus.SUPPORTED,
                evidence_ids=sorted({ev for item in chain for ev in item.evidence_ids}),
            )
        ]
    )

    assert validate_analysis_proposal(state, proposal).accepted


@pytest.mark.asyncio
async def test_unknown_facts_force_candidate_and_preserve_missing_evidence(
    tmp_path: Path,
) -> None:
    state = await _analyze("inference-flow", "candidate", tmp_path)
    unknown = [item for item in state.relations if item.verdict.value == "UNKNOWN"]
    source = next(item for item in unknown if item.relation_type == "controls_workflow_input")
    bad_proposal = AnalysisProposal(
        paths=[
            AttackPath(
                path_id="path_bad_supported",
                nodes=[source.source_id, source.target_id],
                edges=[
                    AttackEdge(
                        source_id=source.source_id,
                        relation_type=source.relation_type,
                        target_id=source.target_id,
                        evidence_ids=source.evidence_ids,
                    )
                ],
                status=FindingStatus.SUPPORTED,
                evidence_ids=source.evidence_ids,
            )
        ]
    )
    rejected = validate_analysis_proposal(state, bad_proposal)
    assert not rejected.accepted
    assert any("UNKNOWN/FALSE" in error for error in rejected.errors)

    invented_relation = bad_proposal.model_copy(deep=True)
    invented_relation.paths[0].path_id = "path_invented_relation"
    invented_relation.paths[0].status = FindingStatus.CANDIDATE
    invented_relation.paths[0].missing_evidence = source.missing_evidence
    invented_relation.paths[0].edges[0].relation_id = "rel_invented"
    invented = validate_analysis_proposal(state, invented_relation)
    assert not invented.accepted
    assert any("unknown relation_id" in error for error in invented.errors)

    candidate = bad_proposal.model_copy(deep=True)
    candidate.paths[0].path_id = "path_candidate"
    candidate.paths[0].status = FindingStatus.CANDIDATE
    candidate.paths[0].missing_evidence = source.missing_evidence
    assert validate_analysis_proposal(state, candidate).accepted


@pytest.mark.asyncio
async def test_secure_fixture_has_guard_without_write_credential(tmp_path: Path) -> None:
    state = await _analyze("inference-flow", "secure", tmp_path)
    assert any(item.category == "guard" for item in state.relations)
    assert not any(
        item.relation_type.startswith("can_use_github_token_")
        for item in state.relations
    )


@pytest.mark.asyncio
async def test_blind_local_action_chain_uses_generic_relations(tmp_path: Path) -> None:
    state = await _analyze("inference-blind", None, tmp_path)
    source = next(
        item for item in state.relations if item.relation_type == "controls_repository_file"
    )
    execution = next(
        item
        for item in state.relations
        if item.relation_type == "repository_file_executes_in"
        and item.source_id == source.target_id
    )
    credential = next(
        item
        for item in state.relations
        if item.relation_type == "can_use_github_token_contents_write"
        and item.source_id == execution.target_id
    )
    assert source.category == "source"
    assert execution.category == "execution"
    assert credential.category == "credential"


@pytest.mark.asyncio
async def test_local_reusable_workflow_is_an_attacker_controlled_source(
    tmp_path: Path,
) -> None:
    state = await _analyze("local-reusable", None, tmp_path)
    source = next(
        item
        for item in state.relations
        if item.relation_type == "controls_repository_file"
        and item.target_id == "file:.github/workflows/reusable.yml"
    )
    execution = next(
        item
        for item in state.relations
        if item.relation_type == "repository_file_executes_in"
        and item.source_id == source.target_id
    )
    assert source.verdict.value == "TRUE"
    assert execution.target_id.endswith(":job:call")


@pytest.mark.asyncio
async def test_security_fact_capability_groups_relations(tmp_path: Path) -> None:
    state = await _analyze("inference-flow", "supported", tmp_path)
    boundary = RepositoryBoundary(FIXTURES / "inference-flow" / "supported")
    payload = await ListSecurityFacts(CapabilityContext(state, boundary))("credential", "TRUE")
    assert "can_use_github_token_contents_write" in payload
    assert '"credential"' in payload


@pytest.mark.asyncio
async def test_oidc_fact_preserves_external_cloud_trust_as_unknown(tmp_path: Path) -> None:
    state = await _analyze("oidc-risk", None, tmp_path)
    cloud = next(
        item
        for item in state.relations
        if item.relation_type == "oidc_may_assume_cloud_identity"
    )
    assert cloud.verdict.value == "UNKNOWN"
    assert cloud.missing_evidence == ["aws federated identity trust policy"]
    assert "aws federated identity trust policy" in state.pending_questions


@pytest.mark.asyncio
async def test_runner_container_install_and_environment_are_unknown_facts(
    tmp_path: Path,
) -> None:
    state = await _analyze("inference-runtime", None, tmp_path)
    relation_types = {item.relation_type for item in state.relations}
    assert "uses_self_hosted_runner" in relation_types
    assert "runner_state_may_reach_job" in relation_types
    assert "mutable_container_image_executes_in_job" in relation_types
    assert "mutable_install_source_executes_in_step" in relation_types
    assert "environment_protection_unknown" in relation_types
    relevant = [
        item
        for item in state.relations
        if item.relation_type
        in {
            "uses_self_hosted_runner",
            "mutable_container_image_executes_in_job",
            "mutable_install_source_executes_in_step",
            "environment_protection_unknown",
        }
    ]
    assert all(item.verdict.value == "UNKNOWN" for item in relevant)


@pytest.mark.asyncio
async def test_checkout_and_github_script_inputs_become_execution_facts(
    tmp_path: Path,
) -> None:
    state = await _analyze("inference-action-input", None, tmp_path)
    relation_types = {item.relation_type for item in state.relations}
    assert "untrusted_input_selects_checkout_revision" in relation_types
    assert "untrusted_input_interpreted_by_github_script" in relation_types


@pytest.mark.asyncio
async def test_gcp_and_azure_oidc_trust_remains_unknown(tmp_path: Path) -> None:
    state = await _analyze("inference-cloud", None, tmp_path)
    cloud = [
        item
        for item in state.relations
        if item.relation_type == "oidc_may_assume_cloud_identity"
    ]
    assert len(cloud) == 2
    assert all(item.verdict.value == "UNKNOWN" for item in cloud)
    missing = {item for relation in cloud for item in relation.missing_evidence}
    assert missing == {
        "azure federated identity trust policy",
        "gcp federated identity trust policy",
    }
