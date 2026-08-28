from __future__ import annotations

from pathlib import Path

import pytest

from cicd_redteam_agent.application.analyze import AnalysisConfig, analyze_repository
from cicd_redteam_agent.domain.graph_frontier import (
    FrontierConnectivity,
    FrontierDisposition,
    FrontierQuery,
    UnknownReasonKind,
    build_attack_frontiers,
)
from cicd_redteam_agent.domain.models import Relation, TriState
from cicd_redteam_agent.domain.proposal_draft import indexed_relations

FIXTURES = Path(__file__).parents[1] / "fixtures"


async def _analyze(name: str, tmp_path: Path):
    report, _ = await analyze_repository(
        FIXTURES / name,
        AnalysisConfig(output_dir=tmp_path / "output"),
    )
    return report.state


def _assert_continuous(state, relation_refs: tuple[str, ...]) -> None:
    relations = dict(indexed_relations(state))
    selected = [relations[reference] for reference in relation_refs]
    assert all(
        previous.target_id == current.source_id
        for previous, current in zip(selected, selected[1:], strict=False)
    )


@pytest.mark.asyncio
async def test_frontier_returns_continuous_proposal_ready_path(tmp_path: Path) -> None:
    state = await _analyze("inference-flow/supported", tmp_path)

    result = build_attack_frontiers(state)
    ready = [item for item in result.frontiers if item.proposal_ready]

    assert ready
    assert any(item.sink_tag == "privileged_execution" for item in ready)
    assert all(item.connectivity is FrontierConnectivity.COMPLETE for item in ready)
    assert any(
        any(value.startswith("workflow_revision_trusted:") for value in item.trust_provenance)
        for item in ready
        if item.job_context_id
    )
    for item in ready:
        _assert_continuous(state, item.relation_refs)


@pytest.mark.asyncio
async def test_trigger_only_route_is_not_proposal_ready(tmp_path: Path) -> None:
    state = await _analyze("inference-flow/supported", tmp_path)

    result = build_attack_frontiers(state)
    trigger_only = [
        item
        for item in result.frontiers
        if item.source_tag == "fork_pr"
        and item.sink_tag == "github_token_write"
        and not item.proposal_ready
    ]

    assert trigger_only
    assert all(item.disposition is FrontierDisposition.EXHAUSTED for item in trigger_only)


@pytest.mark.asyncio
async def test_focused_oidc_frontier_reaches_cloud_identity(tmp_path: Path) -> None:
    state = await _analyze("oidc-risk", tmp_path)
    before = state.model_dump_json()

    result = build_attack_frontiers(
        state,
        FrontierQuery(
            source_hints=("fork_pr",),
            mechanism_hints=("oidc",),
            sink_hints=("cloud_identity",),
        ),
    )
    cloud = next(item for item in result.frontiers if item.sink_tag == "cloud_identity")

    assert cloud.proposal_ready
    assert cloud.objective_score > 0
    assert any(item.kind is UnknownReasonKind.EXTERNAL_REQUIRED for item in cloud.missing)
    _assert_continuous(state, cloud.relation_refs)
    assert state.model_dump_json() == before


@pytest.mark.asyncio
async def test_artifact_frontiers_separate_contents_write_from_oidc(tmp_path: Path) -> None:
    state = await _analyze("global-chain", tmp_path)
    relations = dict(indexed_relations(state))

    result = build_attack_frontiers(state)
    artifact_ready = [
        item
        for item in result.frontiers
        if item.proposal_ready and "artifact" in item.mechanism_tags
    ]

    github_token = next(
        item for item in artifact_ready if item.sink_tag == "github_token_write"
    )
    assert relations[github_token.relation_refs[-1]].relation_type == (
        "can_use_github_token_contents_write"
    )
    assert any(item.sink_tag == "oidc_token" for item in artifact_ready)
    assert all(
        relations[item.relation_refs[-1]].relation_type
        != "can_use_github_token_id-token_write"
        for item in artifact_ready
    )


@pytest.mark.asyncio
async def test_false_checkout_flow_is_terminal_blocked(tmp_path: Path) -> None:
    state = await _analyze("pr-target-blocked", tmp_path)
    relations = dict(indexed_relations(state))

    result = build_attack_frontiers(state)
    blocked = [item for item in result.frontiers if item.disposition is FrontierDisposition.BLOCKED]

    assert blocked
    assert all(not item.proposal_ready for item in blocked)
    assert any(
        any(relations[ref].verdict.value == "FALSE" for ref in item.relation_refs)
        for item in blocked
    )


@pytest.mark.asyncio
async def test_unknown_guard_caps_matching_flow_as_candidate(tmp_path: Path) -> None:
    state = await _analyze("inference-flow/supported", tmp_path)
    execution = next(
        item for item in state.relations if item.relation_type == "env_value_interpreted_as_command"
    )
    state.relations.append(
        Relation(
            source_id=execution.target_id,
            relation_type="execution_condition_present",
            target_id=f"guard:condition:{execution.target_id}",
            category="guard",
            verdict=TriState.UNKNOWN,
            missing_evidence=["condition expression semantics"],
        )
    )

    result = build_attack_frontiers(state)
    guarded = next(
        item
        for item in result.frontiers
        if item.sink_tag == "privileged_execution" and item.proposal_ready
    )

    assert guarded.proposal_status_ceiling.value == "CANDIDATE"
    assert any(item.effect.value == "MAY_BLOCK" for item in guarded.guards)
    assert any(item.kind is UnknownReasonKind.LOCAL_RESOLVABLE for item in guarded.missing)


@pytest.mark.asyncio
async def test_frontier_is_stable_and_reports_result_truncation(tmp_path: Path) -> None:
    state = await _analyze("inference-flow/supported", tmp_path)
    query = FrontierQuery(max_frontiers=1)

    first = build_attack_frontiers(state, query)
    second = build_attack_frontiers(state, query)

    assert first == second
    assert first.truncated
    assert first.search_complete
    assert len(first.frontiers) == 1
