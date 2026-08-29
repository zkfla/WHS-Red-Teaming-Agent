import json
from pathlib import Path

import pytest

from cicd_redteam_agent.application.analyze import AnalysisConfig, analyze_repository
from cicd_redteam_agent.application.objective import (
    compile_objective_goal,
    objective_relation_score,
)
from cicd_redteam_agent.domain.models import (
    AnalysisProposal,
    AttackEdge,
    AttackPath,
    FindingStatus,
    Relation,
    relation_id_for,
)
from cicd_redteam_agent.domain.validator import validate_analysis_proposal
from cicd_redteam_agent.framework.capabilities import (
    CapabilityContext,
    ListAttackFrontiers,
    ListSecurityFacts,
)
from cicd_redteam_agent.security.repository_boundary import RepositoryBoundary

FIXTURES = Path(__file__).parents[1] / "fixtures"


async def _analyze(name: str, variant: str | None, tmp_path: Path):
    repository = FIXTURES / name
    if variant:
        repository /= variant
    report, _ = await analyze_repository(repository, AnalysisConfig(output_dir=tmp_path / "output"))
    return report.state


@pytest.mark.asyncio
async def test_p4_condition_security_uses_one_canonical_relation_path(
    tmp_path: Path,
) -> None:
    state = await _analyze("reasoning-matrix/gha-10", "supported", tmp_path)
    relation_pairs = {(item.relation_type, item.verdict.value) for item in state.relations}
    relation_types = {item.relation_type for item in state.relations}
    assert ("attacker_can_satisfy_condition", "TRUE") in relation_pairs
    assert relation_types.isdisjoint(
        {
            "execution_condition_present",
            "actor_or_event_guard_effectiveness_unknown",
            "event_content_controls_job_condition",
            "label_controls_job_condition",
            "label_or_comment_controls_job_condition",
        }
    )


@pytest.mark.asyncio
async def test_p4_label_guard_is_may_block_until_revision_binding_is_proven(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "label-guard"
    workflows = repository / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "deploy.yml").write_text(
        "on: pull_request_target\n"
        "jobs:\n"
        "  deploy:\n"
        "    if: contains(github.event.pull_request.labels.*.name, 'safe')\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: echo deploy\n",
        encoding="utf-8",
    )
    report, _ = await analyze_repository(
        repository,
        AnalysisConfig(output_dir=tmp_path / "label-output"),
    )
    condition = report.state.condition_evaluations[0]
    guards = [
        item
        for item in report.state.relations
        if item.relation_type == "guard_blocks_untrusted_execution"
        and item.source_id == f"guard:condition:{condition.evaluation_id}"
    ]
    assert condition.revision_bound.value == "UNKNOWN"
    assert len(guards) == 1
    assert guards[0].verdict.value == "UNKNOWN"
    assert "condition approval is not bound to the exact workflow revision" in (
        guards[0].missing_evidence
    )
    assert not any(
        item.relation_type == "attacker_can_satisfy_condition"
        for item in report.state.relations
    )


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
        item for item in state.relations if item.relation_type == "controls_workflow_input"
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
        item.relation_type.startswith("can_use_github_token_") for item in state.relations
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
async def test_pull_request_target_cache_write_is_read_only_in_default_branch_context(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    workflows = repository / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "cache.yml").write_text(
        "on: pull_request_target\npermissions:\n  contents: read\njobs:\n  cache:\n"
        "    runs-on: ubuntu-latest\n    steps:\n"
        "      - uses: actions/cache@2222222222222222222222222222222222222222\n"
        "        with:\n          path: node_modules\n          key: default-branch-cache\n",
        encoding="utf-8",
    )
    report, _ = await analyze_repository(repository, AnalysisConfig(output_dir=tmp_path / "output"))
    cache_access = [
        relation
        for relation in report.state.relations
        if relation.relation_type == "cache_write_access_in_context"
    ]
    assert cache_access
    assert all(relation.verdict.value == "FALSE" for relation in cache_access)
    assert not any(
        relation.relation_type == "attacker_may_populate_cache"
        for relation in report.state.relations
    )


@pytest.mark.asyncio
async def test_security_fact_capability_groups_relations(tmp_path: Path) -> None:
    state = await _analyze("inference-flow", "supported", tmp_path)
    boundary = RepositoryBoundary(FIXTURES / "inference-flow" / "supported")
    payload = await ListSecurityFacts(CapabilityContext(state, boundary))("credential", "TRUE")
    assert "can_use_github_token_contents_write" in payload
    assert '"credential"' in payload


@pytest.mark.asyncio
async def test_security_fact_capability_paginates_large_results(tmp_path: Path) -> None:
    state = await _analyze("inference-flow", "supported", tmp_path)
    boundary = RepositoryBoundary(FIXTURES / "inference-flow" / "supported")
    capability = ListSecurityFacts(CapabilityContext(state, boundary))

    first = json.loads(await capability(limit=1))
    second = json.loads(await capability(offset=first["next_offset"], limit=1))

    assert first["total"] == len([item for item in state.relations if item.category])
    assert len(first["facts"]) == 1
    assert first["next_offset"] == 1
    assert second["offset"] == 1
    assert second["facts"][0]["relation_ref"] != first["facts"][0]["relation_ref"]
    assert "relation_id" not in first["facts"][0]
    assert "evidence_ids" not in first["facts"][0]
    assert first["objective_focus"] is None

    grounded = json.loads(await capability(limit=1, include_grounding=True))
    assert grounded["facts"][0]["relation_id"]
    assert "evidence_ids" in grounded["facts"][0]


@pytest.mark.asyncio
async def test_security_fact_capability_ranks_goal_relevant_relations_first(
    tmp_path: Path,
) -> None:
    state = await _analyze("oidc-risk", None, tmp_path)
    boundary = RepositoryBoundary(FIXTURES / "oidc-risk")
    goal = compile_objective_goal("OIDC와 Cloud Identity 연결 가능성을 우선 조사해")
    capability = ListSecurityFacts(CapabilityContext(state, boundary, objective_goal=goal))

    payload = json.loads(await capability(limit=100))
    returned_scores = [
        objective_relation_score(
            goal,
            source_id=item["source_id"],
            relation_type=item["relation_type"],
            target_id=item["target_id"],
            category=item["category"],
        )
        for item in payload["facts"]
    ]

    assert payload["objective_focus"] == goal.as_dict()
    assert returned_scores == sorted(returned_scores, reverse=True)
    assert returned_scores[0] > 0


@pytest.mark.asyncio
async def test_attack_frontier_capability_paginates_and_exposes_search_limits(
    tmp_path: Path,
) -> None:
    state = await _analyze("oidc-risk", None, tmp_path)
    boundary = RepositoryBoundary(FIXTURES / "oidc-risk")
    goal = compile_objective_goal("fork PR의 OIDC cloud identity 경로를 우선 조사해")
    capability = ListAttackFrontiers(CapabilityContext(state, boundary, objective_goal=goal))

    first = json.loads(await capability(limit=1))
    second = json.loads(await capability(offset=1, limit=1))

    assert first["frontiers"][0]["sink_tag"] == "cloud_identity"
    assert first["frontiers"][0]["proposal_ready"] is True
    assert first["frontiers"][0]["relation_refs"]
    assert first["next_offset"] == 1
    assert second["offset"] == 1
    assert first["frontier_engine_version"]
    assert first["relation_semantics_version"]
    assert isinstance(first["search_complete"], bool)
    assert "truncated" in first


@pytest.mark.asyncio
async def test_attack_frontier_capability_rejects_invalid_page_size(tmp_path: Path) -> None:
    state = await _analyze("inference-flow", "supported", tmp_path)
    boundary = RepositoryBoundary(FIXTURES / "inference-flow" / "supported")

    payload = json.loads(await ListAttackFrontiers(CapabilityContext(state, boundary))(limit=21))

    assert payload == {"error": "invalid_limit", "minimum": 1, "maximum": 20}


@pytest.mark.asyncio
async def test_oidc_fact_preserves_external_cloud_trust_as_unknown(tmp_path: Path) -> None:
    state = await _analyze("oidc-risk", None, tmp_path)
    cloud = next(
        item for item in state.relations if item.relation_type == "oidc_may_assume_cloud_identity"
    )
    assert cloud.verdict.value == "UNKNOWN"
    assert cloud.missing_evidence == [
        "AWS IAM OIDC provider client IDs",
        "AWS IAM role trust policy",
        "GitHub OIDC subject configuration",
        "GitHub repository metadata",
        "configure-aws-credentials authentication mode",
        "fork pull-request OIDC token availability",
    ]
    assert "configure-aws-credentials authentication mode" in state.pending_questions
    assert "fork pull-request OIDC token availability" in state.pending_questions


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
        item for item in state.relations if item.relation_type == "oidc_may_assume_cloud_identity"
    ]
    assert len(cloud) == 2
    assert all(item.verdict.value == "UNKNOWN" for item in cloud)
    missing = {item for relation in cloud for item in relation.missing_evidence}
    assert missing == {
        "azure federated identity trust policy",
        "fork pull-request OIDC token availability",
        "gcp federated identity trust policy",
    }
