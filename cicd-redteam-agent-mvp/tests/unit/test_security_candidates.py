from __future__ import annotations

import json
from pathlib import Path

import pytest

from cicd_redteam_agent.application.analyze import AnalysisConfig, analyze_repository
from cicd_redteam_agent.application.state import create_initial_state
from cicd_redteam_agent.domain.graph_frontier import build_attack_frontiers
from cicd_redteam_agent.domain.investigation_policy import (
    InvestigationTransition,
    PortfolioEntry,
    decide_investigation_policy,
    score_security_candidate,
    select_investigation_portfolio,
)
from cicd_redteam_agent.domain.models import (
    AgentWorkingMemory,
    AnalysisProposal,
    AttackEdge,
    AttackPath,
    CandidateDecisionDraft,
    CandidateDisposition,
    CandidateKind,
    ClaimContract,
    Confidence,
    Evidence,
    EvidenceRequirementKind,
    Finding,
    FindingStatus,
    Relation,
    SecurityCandidate,
    TriState,
    WorkingHypothesis,
    WorkingHypothesisStatus,
)
from cicd_redteam_agent.domain.security_candidate import (
    derive_security_candidates,
    materialize_candidate_decisions,
    materialize_candidate_results,
    reconcile_candidate_state,
)
from cicd_redteam_agent.domain.validator import validate_analysis_proposal
from cicd_redteam_agent.framework.capabilities import CapabilityContext, SubmitAnalysis
from cicd_redteam_agent.framework.working_memory import reconcile_working_memory
from cicd_redteam_agent.security.repository_boundary import RepositoryBoundary

FIXTURES = Path(__file__).parents[1] / "fixtures"
GHA07 = FIXTURES / "reasoning-matrix" / "gha-07" / "supported"
GHA04 = FIXTURES / "reasoning-matrix" / "gha-04" / "supported"


async def _analyze_gha07(tmp_path: Path):
    report, _ = await analyze_repository(
        GHA07,
        AnalysisConfig(output_dir=tmp_path / "gha07"),
    )
    return report.state


def _candidate_projection(candidates):
    return [
        (
            candidate.candidate_id,
            candidate.candidate_kind.value,
            candidate.root_cause_family,
            candidate.root_cause_key,
            candidate.proposal_status_ceiling.value,
            candidate.status.value,
            tuple(candidate.supporting_relation_ids),
        )
        for candidate in candidates
    ]


def _reasoning_initial_state(state):
    reasoning_state = state.model_copy(deep=True)
    reasoning_state.findings = []
    reasoning_state.paths = []
    reasoning_state.candidates = []
    reasoning_state.coverage_ledger = []
    reasoning_state.candidates, reasoning_state.coverage_ledger = reconcile_candidate_state(
        (), derive_security_candidates(reasoning_state)
    )
    return reasoning_state


@pytest.mark.asyncio
async def test_deterministic_materialization_and_reasoning_candidate_parity(tmp_path: Path) -> None:
    state = await _analyze_gha07(tmp_path)
    reasoning_state = _reasoning_initial_state(state)

    assert _candidate_projection(state.candidates) == _candidate_projection(
        reasoning_state.candidates
    )
    materialized_findings, materialized_paths = materialize_candidate_results(state.candidates)
    assert materialized_findings
    assert all(finding.candidate_id for finding in materialized_findings)
    assert all(path.candidate_id for path in materialized_paths)
    assert {finding.finding_id for finding in materialized_findings} <= {
        finding.finding_id for finding in state.findings
    }
    assert {path.path_id for path in materialized_paths} <= {
        path.path_id for path in state.paths
    }


@pytest.mark.asyncio
async def test_mutable_action_candidate_is_grounded_in_action_relations_and_requirements(
    tmp_path: Path,
) -> None:
    report, _ = await analyze_repository(
        GHA04,
        AnalysisConfig(output_dir=tmp_path / "gha04"),
    )
    state = report.state
    candidates = [
        candidate
        for candidate in state.candidates
        if candidate.candidate_kind is CandidateKind.MUTABLE_ACTION
    ]
    assert candidates
    relation_by_id = {relation.relation_id: relation for relation in state.relations}

    for candidate in candidates:
        assert candidate.supporting_relation_ids
        grounding_ids = {
            *candidate.supporting_relation_ids,
            *candidate.contradicting_relation_ids,
            *candidate.guards,
        }
        assert grounding_ids <= set(relation_by_id)
        grounding_relations = [relation_by_id[relation_id] for relation_id in grounding_ids]
        grounding_types = {relation.relation_type for relation in grounding_relations}
        assert "contains_job" not in grounding_types
        assert "contains_step" not in grounding_types
        assert {
            "uses_external_action_ref",
            "external_code_may_execute_in_step",
        } <= grounding_types
        assert {
            "uses_external_action_ref",
            "external_code_may_execute_in_step",
        } <= set(candidate.claim_contract.required_relation_types)

        expected_requirements = {
            (item.kind, item.provider, item.subject_id, item.qualifier)
            for relation in grounding_relations
            for item in relation.evidence_requirements
        }
        actual_requirements = {
            (item.kind, item.provider, item.subject_id, item.qualifier)
            for item in candidate.evidence_requirements
        }
        assert expected_requirements <= actual_requirements
        assert {
            EvidenceRequirementKind.GITHUB_ACTION_PROVENANCE,
            EvidenceRequirementKind.GITHUB_ACTION_ADVISORY,
            EvidenceRequirementKind.GITHUB_ACTIONS_POLICY,
        } <= {item.kind for item in candidate.evidence_requirements}


@pytest.mark.asyncio
async def test_full_action_sha_is_not_a_generic_mutable_action_candidate(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "full-sha"
    workflow_dir = repository / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "build.yml").write_text(
        """name: Full SHA
on: push
permissions: write-all
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
""",
        encoding="utf-8",
    )

    report, _ = await analyze_repository(
        repository,
        AnalysisConfig(output_dir=tmp_path / "full-sha-output"),
    )

    assert not any(
        finding.rule_id == "GHA-04-MUTABLE-EXTERNAL-ACTION"
        for finding in report.state.findings
    )
    assert not any(
        candidate.candidate_kind is CandidateKind.MUTABLE_ACTION
        for candidate in report.state.candidates
    )


@pytest.mark.asyncio
async def test_action_ref_root_cause_identity_is_shared_and_distinct_by_ref(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "root-cause-identity"
    workflow_dir = repository / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    for name, action_ref in (
        ("first.yml", "actions/checkout@v3"),
        ("second.yml", "actions/checkout@v3"),
        ("third.yml", "actions/checkout@v4"),
    ):
        (workflow_dir / name).write_text(
            f"""name: {name}
on: push
permissions: write-all
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: {action_ref}
""",
            encoding="utf-8",
        )

    report, _ = await analyze_repository(
        repository,
        AnalysisConfig(output_dir=tmp_path / "root-cause-output"),
    )
    candidates = [
        candidate
        for candidate in report.state.candidates
        if candidate.candidate_kind is CandidateKind.MUTABLE_ACTION
    ]

    assert len(candidates) == 3
    assert {candidate.root_cause_family for candidate in candidates} == {
        "mutable-action-reference"
    }
    assert sum(
        candidate.root_cause_key == "mutable-action-reference:actions/checkout@v3"
        for candidate in candidates
    ) == 2
    assert sum(
        candidate.root_cause_key == "mutable-action-reference:actions/checkout@v4"
        for candidate in candidates
    ) == 1
    assert len({candidate.candidate_id for candidate in candidates}) == 3


@pytest.mark.asyncio
async def test_secret_log_candidate_materializes_only_its_bounded_claim(tmp_path: Path) -> None:
    state = _reasoning_initial_state(await _analyze_gha07(tmp_path))
    candidate = next(
        item for item in state.candidates if item.candidate_kind is CandidateKind.SECRET_LOG
    )
    variant = candidate.path_variants[0]
    decision = CandidateDecisionDraft(
        candidate_id=candidate.candidate_id,
        decision=CandidateDisposition.ACCEPTED,
        selected_path_variant=variant.path_id,
        confidence=Confidence.HIGH,
    )

    proposal, _ = materialize_candidate_decisions(state, [decision])
    finding = proposal.findings[0]
    assert finding.candidate_id == candidate.candidate_id
    assert finding.summary == candidate.claim_contract.bounded_summary
    assert "does not claim that github masking failed" in finding.summary.casefold()
    assert validate_analysis_proposal(state, proposal).accepted

    overclaimed = proposal.model_copy(deep=True)
    overclaimed.findings[0].summary = "Plaintext secret leaked because GitHub masking failed."
    validation = validate_analysis_proposal(state, overclaimed)
    assert not validation.accepted
    assert any("forbidden claim language" in error for error in validation.errors)


@pytest.mark.asyncio
async def test_broad_portfolio_requires_terminal_coverage_but_focused_candidate_can_submit(
    tmp_path: Path,
) -> None:
    original = await _analyze_gha07(tmp_path)
    state = _reasoning_initial_state(original)
    result = build_attack_frontiers(state)
    broad = decide_investigation_policy(result, state)

    assert len(broad.portfolio) == 6
    assert sum(entry.candidate is not None for entry in broad.portfolio) >= 2
    assert not broad.candidate_coverage_complete

    first_candidate = next(entry.candidate for entry in broad.portfolio if entry.candidate)
    context = CapabilityContext(
        state=state,
        boundary=RepositoryBoundary(GHA07),
        investigation_policy=broad,
    )
    response = json.loads(
        await SubmitAnalysis(context)(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "candidate_decisions": [
                        {
                            "candidate_id": first_candidate.candidate_id,
                            "decision": "REJECTED",
                        }
                    ],
                }
            )
        )
    )
    assert not response["accepted"]
    assert any(
        "selected_candidate_without_terminal_disposition" in error
        or "frontier_decisions_required_for_selected_portfolio" in error
        for error in response["errors"]
    )

    focused_candidate = next(
        candidate
        for candidate in state.candidates
        if candidate.candidate_kind is CandidateKind.SECRET_LOG
    )
    focused_entry = PortfolioEntry(
        rank=1,
        score=score_security_candidate(focused_candidate, state),
        candidate=focused_candidate,
    )
    focused = decide_investigation_policy(
        result,
        state,
        focused=True,
        selected_portfolio=(focused_entry,),
    )
    assert focused.transition is InvestigationTransition.INVESTIGATE
    assert focused.decision_submission_allowed
    assert len(focused.portfolio) == 1
    assert focused.portfolio[0].candidate is not None


@pytest.mark.asyncio
async def test_mixed_candidate_batch_accepts_frontier_with_exact_path_only(tmp_path: Path) -> None:
    """Mirror the live-model mixed batch contract without legacy Finding prose."""

    state = _reasoning_initial_state(await _analyze_gha07(tmp_path))
    result = build_attack_frontiers(state)
    policy = decide_investigation_policy(result, state)
    selected_candidates = [entry.candidate for entry in policy.portfolio if entry.candidate]
    selected_frontiers = [entry.frontier for entry in policy.portfolio if entry.frontier]
    assert selected_candidates
    assert selected_frontiers
    accepted_frontier = selected_frontiers[0]
    context = CapabilityContext(
        state=state,
        boundary=RepositoryBoundary(GHA07),
        investigation_policy=policy,
    )

    response = json.loads(
        await SubmitAnalysis(context)(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "candidate_decisions": [
                        {"candidate_id": candidate.candidate_id, "decision": "REJECTED"}
                        for candidate in selected_candidates
                    ],
                    "frontier_decisions": [
                        {
                            "frontier_id": frontier.frontier_id,
                            "decision": "ACCEPTED" if frontier is accepted_frontier else "MERGED",
                            "confidence": "HIGH",
                        }
                        for frontier in selected_frontiers
                    ],
                    "path_drafts": [
                        {
                            "path_id": "path_mixed_frontier",
                            "relation_refs": list(accepted_frontier.relation_refs),
                        }
                    ],
                }
            )
        )
    )

    assert response["accepted"]
    assert response["batch_committed"]
    assert not response["analysis_complete"]
    assert any(path.path_id == "path_mixed_frontier" for path in state.paths)


@pytest.mark.asyncio
async def test_broad_root_cause_coverage_rotates_to_next_batch(tmp_path: Path) -> None:
    repository = tmp_path / "multi-batch"
    workflow_dir = repository / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    actions = [
        "actions/checkout@v3",
        "actions/setup-node@v3",
        "actions/cache@v3",
        "docker/setup-buildx-action@v2",
        "docker/login-action@v2",
        "docker/build-push-action@v4",
        "github/codeql-action/init@v2",
        "github/codeql-action/analyze@v2",
    ]
    for index, action_ref in enumerate(actions):
        workflow_text = (
            f"name: w{index}\non: push\npermissions: write-all\njobs:\n"
            "  build:\n    runs-on: ubuntu-latest\n    steps:\n"
            f"      - uses: {action_ref}\n"
        )
        (workflow_dir / f"w{index}.yml").write_text(
            workflow_text,
            encoding="utf-8",
        )

    report, _ = await analyze_repository(
        repository,
        AnalysisConfig(output_dir=tmp_path / "multi-batch-output"),
    )
    state = _reasoning_initial_state(report.state)
    result = build_attack_frontiers(state)
    first = select_investigation_portfolio(result, state, limit=3)
    first_candidates = [entry.candidate for entry in first if entry.candidate is not None]
    assert len(first_candidates) == 3
    first_roots = {candidate.root_cause_key for candidate in first_candidates}

    context = CapabilityContext(
        state=state,
        boundary=RepositoryBoundary(repository),
        investigation_policy=decide_investigation_policy(
            result,
            state,
            portfolio_limit=3,
            selected_portfolio=first,
        ),
    )
    response = json.loads(
        await SubmitAnalysis(context)(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "candidate_decisions": [
                        {"candidate_id": candidate.candidate_id, "decision": "REJECTED"}
                        for candidate in first_candidates
                    ],
                    "frontier_decisions": [
                        {"frontier_id": entry.frontier.frontier_id, "decision": "REJECTED"}
                        for entry in first
                        if entry.frontier is not None
                    ],
                }
            )
        )
    )
    assert response["accepted"]
    assert response["batch_committed"]
    assert not response["analysis_complete"]
    assert not context.submitted

    second = select_investigation_portfolio(result, state, limit=3)
    second_roots = {
        entry.candidate.root_cause_key
        for entry in second
        if entry.candidate is not None
    }
    assert second_roots
    assert first_roots.isdisjoint(second_roots)


@pytest.mark.asyncio
async def test_same_root_cause_occurrences_are_covered_by_one_representative(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "same-root"
    workflow_dir = repository / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    for index in range(2):
        workflow_text = (
            f"name: w{index}\non: push\npermissions: write-all\njobs:\n"
            "  build:\n    runs-on: ubuntu-latest\n    steps:\n"
            "      - uses: actions/checkout@v3\n"
        )
        (workflow_dir / f"w{index}.yml").write_text(
            workflow_text,
            encoding="utf-8",
        )
    report, _ = await analyze_repository(
        repository,
        AnalysisConfig(output_dir=tmp_path / "same-root-output"),
    )
    state = _reasoning_initial_state(report.state)
    result = build_attack_frontiers(state)
    portfolio = select_investigation_portfolio(result, state, limit=6)
    representatives = [
        entry.candidate
        for entry in portfolio
        if entry.candidate is not None
        and entry.candidate.root_cause_key == "mutable-action-reference:actions/checkout@v3"
    ]
    assert len(representatives) == 1

    context = CapabilityContext(
        state=state,
        boundary=RepositoryBoundary(repository),
        investigation_policy=decide_investigation_policy(
            result,
            state,
            selected_portfolio=portfolio,
        ),
    )
    payload = {
        "schema_version": "1.0",
        "candidate_decisions": [
            {"candidate_id": entry.candidate.candidate_id, "decision": "REJECTED"}
            for entry in portfolio
            if entry.candidate is not None
        ],
        "frontier_decisions": [
            {"frontier_id": entry.frontier.frontier_id, "decision": "REJECTED"}
            for entry in portfolio
            if entry.frontier is not None
        ],
    }
    response = json.loads(await SubmitAnalysis(context)(json.dumps(payload)))
    assert response["accepted"]
    same_root = [
        candidate
        for candidate in state.candidates
        if candidate.root_cause_key == "mutable-action-reference:actions/checkout@v3"
    ]
    assert len(same_root) == 2
    assert all(candidate.disposition is CandidateDisposition.REJECTED for candidate in same_root)


def _hard_negative_state(tmp_path: Path):
    repository = tmp_path / "hard-negative"
    repository.mkdir()
    boundary = RepositoryBoundary(repository)
    state = create_initial_state(boundary)
    evidence_id = "ev_hard_negative"
    state.evidence[evidence_id] = Evidence(
        evidence_id=evidence_id,
        kind="test",
        source_path=".github/workflows/ci.yml",
        line_start=1,
        line_end=1,
        field_path="$.jobs.build.steps[0]",
        redacted_excerpt="fork revision executes repository file",
        content_hash="a" * 64,
    )
    relations = [
        Relation(
            source_id="attacker:fork-pr",
            relation_type="attacker_selects_current_pull_request_head",
            target_id="workspace:pr-head",
            category="source",
            verdict=TriState.TRUE,
            evidence_ids=[evidence_id],
        ),
        Relation(
            source_id="workspace:pr-head",
            relation_type="repository_file_executes_in",
            target_id="step:build",
            category="execution",
            verdict=TriState.TRUE,
            evidence_ids=[evidence_id],
        ),
    ]
    state.relations = relations
    path = AttackPath(
        path_id="path_hard_negative",
        nodes=["attacker:fork-pr", "workspace:pr-head", "step:build"],
        edges=[
            AttackEdge(
                relation_id=relation.relation_id,
                source_id=relation.source_id,
                relation_type=relation.relation_type,
                target_id=relation.target_id,
                evidence_ids=[evidence_id],
            )
            for relation in relations
        ],
        status=FindingStatus.SUPPORTED,
        candidate_id="candidate_hard_negative",
        evidence_ids=[evidence_id],
    )
    contract = ClaimContract(
        contract_id="claim_hard_negative",
        rule_id="CICD-UNTRUSTED-PRIVILEGED-EXECUTION",
        title="Repository-controlled code may execute in a privileged job",
        bounded_summary=(
            "Local evidence links an untrusted change path, repository-controlled execution, "
            "and a privileged job."
        ),
        proposal_status_ceiling=FindingStatus.SUPPORTED,
        required_relation_types=[relation.relation_type for relation in relations],
    )
    state.candidates = [
        SecurityCandidate(
            candidate_id="candidate_hard_negative",
            source_finding_id="finding_hard_negative",
            candidate_kind=CandidateKind.UNTRUSTED_PRIVILEGED_EXECUTION,
            root_cause_key="untrusted-privileged-execution",
            claim_contract=contract,
            proposal_status_ceiling=FindingStatus.SUPPORTED,
            status=FindingStatus.SUPPORTED,
            confidence=Confidence.HIGH,
            supporting_relation_ids=[relation.relation_id for relation in relations],
            path_variants=[path],
        )
    ]
    finding = Finding(
        finding_id="finding_hard_negative",
        rule_id=contract.rule_id,
        title=contract.title,
        status=FindingStatus.SUPPORTED,
        candidate_id="candidate_hard_negative",
        evidence_ids=[evidence_id],
        confidence=Confidence.HIGH,
        summary=contract.bounded_summary,
    )
    return state, AnalysisProposal(findings=[finding], paths=[path])


def test_fork_revision_execution_without_privilege_cannot_be_supported_privileged(
    tmp_path: Path,
) -> None:
    state, proposal = _hard_negative_state(tmp_path)

    validation = validate_analysis_proposal(state, proposal)

    assert not validation.accepted
    assert any("explicit TRUE privilege relation" in error for error in validation.errors)


@pytest.mark.asyncio
async def test_recompute_blocks_disappeared_candidate_and_stale_working_memory(
    tmp_path: Path,
) -> None:
    state = await _analyze_gha07(tmp_path)
    candidate = next(
        item for item in state.candidates if item.candidate_kind is CandidateKind.SECRET_LOG
    )
    support = candidate.supporting_relation_ids[:2]
    state.working_memory = AgentWorkingMemory(
        revision=1,
        focus_hypothesis_id="hyp_aaaaaaaaaaaaaaaa",
        hypotheses=[
            WorkingHypothesis(
                hypothesis_id="hyp_aaaaaaaaaaaaaaaa",
                candidate_id=candidate.candidate_id,
                claim="Secret log candidate under review",
                status=WorkingHypothesisStatus.ACTIVE,
                supporting_relation_ids=support,
                evidence_ids=candidate.evidence_ids,
                decision_summary="Awaiting external semantic recompute.",
                created_revision=1,
                updated_revision=1,
            )
        ],
    )
    state.candidates, state.coverage_ledger = reconcile_candidate_state(
        [candidate],
        [],
        state.coverage_ledger,
    )
    frontiers = build_attack_frontiers(state)
    reconciliation = reconcile_working_memory(state, frontiers)
    policy = decide_investigation_policy(frontiers, state)

    assert reconciliation.changed
    blocked_entry = next(
        entry for entry in state.coverage_ledger if entry.item_id == candidate.candidate_id
    )
    assert blocked_entry.disposition is CandidateDisposition.BLOCKED_EXTERNAL
    assert state.working_memory.hypotheses[0].status is WorkingHypothesisStatus.DISCARDED
    assert state.working_memory.focus_hypothesis_id is None
    assert all(entry.item_id != candidate.candidate_id for entry in policy.portfolio)
    assert any(
        entry.item_id == candidate.candidate_id
        and entry.disposition is CandidateDisposition.BLOCKED_EXTERNAL
        for entry in state.coverage_ledger
    )
