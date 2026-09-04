from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from cicd_redteam_agent.application.analyze import AnalysisConfig, analyze_repository
from cicd_redteam_agent.application.state import create_initial_state
from cicd_redteam_agent.domain.graph_frontier import (
    FrontierQuery,
    build_attack_frontiers,
)
from cicd_redteam_agent.domain.investigation_policy import (
    InvestigationRuntimeState,
    InvestigationTransition,
    decide_investigation_policy,
    select_frontier_portfolio,
)
from cicd_redteam_agent.domain.models import (
    AgentWorkingMemory,
    CandidateDisposition,
    WorkingHypothesis,
    WorkingHypothesisStatus,
)
from cicd_redteam_agent.domain.proposal_draft import indexed_relations
from cicd_redteam_agent.framework.capabilities import (
    CapabilityContext,
    ListAttackFrontiers,
    SubmitAnalysis,
)
from cicd_redteam_agent.framework.working_memory import reconcile_working_memory
from cicd_redteam_agent.security.repository_boundary import RepositoryBoundary

FIXTURES = Path(__file__).parents[1] / "fixtures"


async def _analyze(name: str, tmp_path: Path):
    report, _ = await analyze_repository(
        FIXTURES / name,
        AnalysisConfig(output_dir=tmp_path / name.replace("/", "-")),
    )
    report.state.findings = []
    report.state.paths = []
    return report.state


@pytest.mark.asyncio
async def test_scoring_and_portfolio_are_stable_and_bounded(tmp_path: Path) -> None:
    state = await _analyze("global-chain", tmp_path)
    result = build_attack_frontiers(state)

    first = select_frontier_portfolio(result, state, limit=4)
    second = select_frontier_portfolio(result, state, limit=4)

    assert first == second
    assert 1 <= len(first) <= 4
    assert [entry.rank for entry in first] == list(range(1, len(first) + 1))
    assert len({entry.frontier.frontier_id for entry in first}) == len(first)
    assert all(entry.score.frontier_id == entry.frontier.frontier_id for entry in first)
    assert all(
        entry.score.total == sum(entry.score.as_dict()["breakdown"].values()) for entry in first
    )

    source = next(item for item in result.frontiers if item.disposition.value != "BLOCKED")
    duplicated = replace(
        result,
        frontiers=(source, replace(source, frontier_id="frontier_ffffffffffffffff")),
    )
    deduplicated = select_frontier_portfolio(duplicated, state, limit=6)
    assert len(deduplicated) == 1


@pytest.mark.asyncio
async def test_transition_prefers_observation_then_submit_or_investigation(
    tmp_path: Path,
) -> None:
    state = await _analyze("inference-flow/supported", tmp_path)
    complete = build_attack_frontiers(state)

    observing = decide_investigation_policy(complete, state, observation_ready=True)
    submitting = decide_investigation_policy(complete, state)
    incomplete = build_attack_frontiers(state, FrontierQuery(max_expansions=0))
    investigating = decide_investigation_policy(incomplete, state)

    assert observing.transition is InvestigationTransition.OBSERVE
    assert submitting.transition is InvestigationTransition.INVESTIGATE
    assert submitting.decision_submission_allowed
    assert submitting.allowed_relation_ref_paths
    assert investigating.transition is InvestigationTransition.INVESTIGATE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runtime", "expected_reason"),
    [
        (InvestigationRuntimeState(limit_reached=True, limit_reason="max_rounds"), "max_rounds"),
        (
            InvestigationRuntimeState(
                tokens_remaining=1_000,
                predicted_next_round_tokens=20_000,
            ),
            "insufficient_token_reserve",
        ),
        (InvestigationRuntimeState(no_progress_rounds=4), "no_progress"),
    ],
)
async def test_runtime_pressure_forces_stop_partial_before_other_transitions(
    tmp_path: Path,
    runtime: InvestigationRuntimeState,
    expected_reason: str,
) -> None:
    state = await _analyze("inference-flow/supported", tmp_path)
    result = build_attack_frontiers(state)

    policy = decide_investigation_policy(
        result,
        state,
        observation_ready=True,
        runtime=runtime,
    )

    assert policy.transition is InvestigationTransition.STOP_PARTIAL
    assert policy.stop_reason == expected_reason
    assert not policy.empty_submission_allowed
    assert policy.as_dict()["stop_reason"] == expected_reason


@pytest.mark.asyncio
async def test_frontier_capability_exposes_ranked_policy_portfolio(tmp_path: Path) -> None:
    state = await _analyze("inference-flow/supported", tmp_path)
    boundary = RepositoryBoundary(FIXTURES / "inference-flow" / "supported")

    payload = json.loads(await ListAttackFrontiers(CapabilityContext(state, boundary))())

    policy = payload["investigation_policy"]
    assert policy["transition"] == "INVESTIGATE"
    assert policy["decision_submission_allowed"]
    assert policy["portfolio"]
    assert policy["portfolio"][0]["portfolio_rank"] == 1
    assert "candidate_score" in policy["portfolio"][0]


@pytest.mark.asyncio
async def test_reconcile_marks_ready_and_discards_duplicate_grounding(tmp_path: Path) -> None:
    state = await _analyze("inference-flow/supported", tmp_path)
    result = build_attack_frontiers(state)
    frontier = next(item for item in result.frontiers if item.proposal_ready)
    relation_by_ref = dict(indexed_relations(state))
    relations = [relation_by_ref[reference] for reference in frontier.relation_refs]
    relation_ids = [relation.relation_id for relation in relations]
    evidence_ids = list(
        dict.fromkeys(
            evidence_id for relation in relations for evidence_id in relation.evidence_ids
        )
    )
    missing = list(
        dict.fromkeys(item for relation in relations for item in relation.missing_evidence)
    )
    state.working_memory = AgentWorkingMemory(
        revision=1,
        focus_hypothesis_id="hyp_1111111111111111",
        hypotheses=[
            WorkingHypothesis(
                hypothesis_id="hyp_1111111111111111",
                claim="Primary grounded path",
                supporting_relation_ids=relation_ids,
                evidence_ids=evidence_ids,
                missing_evidence=missing,
                decision_summary="Needs canonical reconciliation.",
                created_revision=1,
                updated_revision=1,
            ),
            WorkingHypothesis(
                hypothesis_id="hyp_2222222222222222",
                claim="Duplicate wording for the same path",
                supporting_relation_ids=relation_ids,
                evidence_ids=evidence_ids,
                missing_evidence=missing,
                decision_summary="This repeats the same grounded investigation.",
                created_revision=1,
                updated_revision=1,
            ),
        ],
    )

    reconciliation = reconcile_working_memory(state, result)

    assert reconciliation.changed
    assert state.working_memory.revision == 2
    assert state.working_memory.hypotheses[0].status is WorkingHypothesisStatus.READY_FOR_PROPOSAL
    assert state.working_memory.hypotheses[1].status is WorkingHypothesisStatus.DISCARDED
    assert state.working_memory.focus_hypothesis_id == "hyp_1111111111111111"
    assert not reconcile_working_memory(state, result).changed


@pytest.mark.asyncio
async def test_reconcile_does_not_discard_uncovered_hypothesis_when_search_is_truncated(
    tmp_path: Path,
) -> None:
    state = await _analyze("inference-flow/supported", tmp_path)
    result = build_attack_frontiers(state)
    relation = next(
        item for item in state.relations if item.evidence_ids and item.verdict.value == "TRUE"
    )
    state.working_memory = AgentWorkingMemory(
        revision=1,
        focus_hypothesis_id="hyp_4444444444444444",
        hypotheses=[
            WorkingHypothesis(
                hypothesis_id="hyp_4444444444444444",
                claim="Grounded relation omitted from a truncated Frontier result",
                supporting_relation_ids=[relation.relation_id],
                evidence_ids=relation.evidence_ids,
                missing_evidence=relation.missing_evidence,
                decision_summary="Awaiting a complete Frontier search.",
                created_revision=1,
                updated_revision=1,
            )
        ],
    )

    truncated = replace(
        result,
        frontiers=(),
        search_complete=True,
        truncated=True,
    )
    reconciliation = reconcile_working_memory(state, truncated)

    assert not reconciliation.changed
    assert state.working_memory.hypotheses[0].status is WorkingHypothesisStatus.ACTIVE

    complete = replace(truncated, truncated=False)
    reconciliation = reconcile_working_memory(state, complete)

    assert reconciliation.changed
    assert state.working_memory.hypotheses[0].status is WorkingHypothesisStatus.DISCARDED


@pytest.mark.asyncio
async def test_reconcile_discards_hypothesis_on_blocked_frontier(tmp_path: Path) -> None:
    state = await _analyze("pr-target-blocked", tmp_path)
    result = build_attack_frontiers(state)
    blocked = next(item for item in result.frontiers if item.disposition.value == "BLOCKED")
    relation_by_ref = dict(indexed_relations(state))
    relations = [relation_by_ref[reference] for reference in blocked.relation_refs]
    state.working_memory = AgentWorkingMemory(
        revision=1,
        focus_hypothesis_id="hyp_3333333333333333",
        hypotheses=[
            WorkingHypothesis(
                hypothesis_id="hyp_3333333333333333",
                claim="Blocked checkout reaches privileged execution",
                supporting_relation_ids=[relation.relation_id for relation in relations],
                evidence_ids=list(
                    dict.fromkeys(
                        evidence_id
                        for relation in relations
                        for evidence_id in relation.evidence_ids
                    )
                ),
                missing_evidence=list(
                    dict.fromkeys(
                        item for relation in relations for item in relation.missing_evidence
                    )
                ),
                decision_summary="Awaiting current Frontier reconciliation.",
                created_revision=1,
                updated_revision=1,
            )
        ],
    )

    reconciliation = reconcile_working_memory(state, result)

    assert reconciliation.changed
    hypothesis = state.working_memory.hypotheses[0]
    assert hypothesis.status is WorkingHypothesisStatus.DISCARDED
    assert hypothesis.next_check is None
    assert state.working_memory.focus_hypothesis_id is None


@pytest.mark.asyncio
async def test_submission_gate_rejects_stop_partial_even_with_grounded_paths(
    tmp_path: Path,
) -> None:
    state = await _analyze("inference-flow/supported", tmp_path)
    result = build_attack_frontiers(state)
    policy = decide_investigation_policy(
        result,
        state,
        runtime=InvestigationRuntimeState(limit_reached=True, limit_reason="max_tokens"),
    )
    context = CapabilityContext(
        state,
        RepositoryBoundary(FIXTURES / "inference-flow" / "supported"),
        investigation_policy=policy,
    )

    rejected = json.loads(
        await SubmitAnalysis(context)(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "path_drafts": [],
                    "finding_drafts": [],
                }
            )
        )
    )

    assert not rejected["accepted"]
    assert rejected["errors"] == ["submission_transition_not_ready:STOP_PARTIAL"]
    assert not context.submitted


@pytest.mark.asyncio
async def test_submission_gate_accepts_only_selected_portfolio_path(tmp_path: Path) -> None:
    state = await _analyze("inference-flow/supported", tmp_path)
    result = build_attack_frontiers(state)
    policy = decide_investigation_policy(result, state)
    selected = policy.allowed_relation_ref_paths[0]
    context = CapabilityContext(
        state,
        RepositoryBoundary(FIXTURES / "inference-flow" / "supported"),
        investigation_policy=replace(policy, allowed_relation_ref_paths=(selected,)),
    )
    proposal = {
        "schema_version": "1.0",
        "path_drafts": [{"path_id": "path_selected", "relation_refs": list(selected)}],
        "finding_drafts": [
            {
                "finding_id": "finding_selected",
                "rule_id": "POLICY_SELECTED_PATH",
                "title": "Selected portfolio path",
                "path_id": "path_selected",
                "confidence": "MEDIUM",
                "summary": "The deterministic policy selected this grounded path.",
            }
        ],
    }

    accepted = json.loads(await SubmitAnalysis(context)(json.dumps(proposal)))

    assert accepted["accepted"]
    assert context.submitted


@pytest.mark.asyncio
async def test_submission_gate_rejects_early_and_allows_proven_empty(
    tmp_path: Path,
) -> None:
    state = await _analyze("inference-flow/supported", tmp_path)
    incomplete = build_attack_frontiers(state, FrontierQuery(max_expansions=0))
    context = CapabilityContext(
        state,
        RepositoryBoundary(FIXTURES / "inference-flow" / "supported"),
        investigation_policy=decide_investigation_policy(incomplete, state),
    )

    early = json.loads(await SubmitAnalysis(context)('{"schema_version":"1.0"}'))

    assert not early["accepted"]
    assert early["errors"] == ["submission_transition_not_ready:INVESTIGATE"]

    empty_repository = tmp_path / "empty"
    empty_repository.mkdir()
    empty_state = create_initial_state(RepositoryBoundary(empty_repository))
    empty_result = build_attack_frontiers(empty_state)
    empty_policy = decide_investigation_policy(empty_result, empty_state)
    empty_context = CapabilityContext(
        empty_state,
        RepositoryBoundary(empty_repository),
        investigation_policy=empty_policy,
    )

    accepted = json.loads(await SubmitAnalysis(empty_context)('{"schema_version":"1.0"}'))

    assert empty_policy.empty_submission_allowed
    assert accepted["accepted"]


@pytest.mark.asyncio
async def test_repeated_capability_calls_alone_do_not_stop_rotating_candidate_batches(
    tmp_path: Path,
) -> None:
    state = await _analyze("inference-flow/supported", tmp_path)
    result = build_attack_frontiers(state)
    policy = decide_investigation_policy(
        result,
        state,
        runtime=InvestigationRuntimeState(repeated_calls=20, no_progress_rounds=0),
    )
    assert policy.transition is not InvestigationTransition.STOP_PARTIAL


@pytest.mark.asyncio
async def test_candidate_portfolio_is_candidate_only_and_diversifies_root_families(
    tmp_path: Path,
) -> None:
    state = await _analyze("reasoning-matrix/gha-07/supported", tmp_path)
    for candidate in state.candidates:
        candidate.disposition = CandidateDisposition.UNREVIEWED
    state.coverage_ledger = []
    result = build_attack_frontiers(state)
    policy = decide_investigation_policy(result, state)
    candidates = [entry.candidate for entry in policy.portfolio if entry.candidate is not None]

    assert candidates
    assert all(entry.frontier is None for entry in policy.portfolio)
    families = [candidate.root_cause_family for candidate in candidates]
    # If multiple families are available, each gets one slot before a duplicate family consumes one.
    available_families = {candidate.root_cause_family for candidate in state.candidates}
    assert len(set(families)) == min(len(candidates), len(available_families))
