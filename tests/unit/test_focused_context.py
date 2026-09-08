import json
from datetime import UTC, datetime

import pytest

from redteam_core.investigation import Observation
from redteam_core.knowledge import CoreSession
from redteam_core.models import (
    ClaimContract,
    ClaimStatus,
    ExpectationRef,
    Provenance,
    Relation,
    RootCauseRef,
    SecurityCandidate,
    TriState,
    TrustClass,
)
from redteam_core.reasoning import (
    ContextBundleStatus,
    FocusedContextBudgetExceeded,
    FocusedContextPolicy,
    project_focused_decision_context,
)
from redteam_core.reasoning.focused_context import project_focused_context_retrieval
from tests.helpers import SamplePayload, catalog, requirement

CANDIDATE_TYPE = "reference.candidate.value@1"
RELATION_TYPE = "reference.relation.focus@1"


def test_historical_retrieval_is_rebuilt_from_current_canonical_state():
    session, candidate, _, _ = fixture()
    historical = project_focused_context_retrieval(
        session, retrieval_ref=f"candidate:{candidate.candidate_id}:relations"
    )
    relation = session.semantic.relations[candidate.supporting_relation_ids[0]]
    relation.verdict = TriState.FALSE
    forged = json.loads(historical.content)
    forged["items"] = [{"verdict": "FORGED_TRUE"}]
    result = project_focused_decision_context(
        session,
        active_candidate_ids=(candidate.candidate_id,),
        prior_retrieval_contents=(json.dumps(forged),),
    )
    rows = json.loads(result.content)["channels"]["CANONICAL_STATE"]["retrieval_results"]
    actual = next(row for row in rows[0]["items"] if row["relation_id"] == relation.relation_id)
    assert actual["verdict"] == "FALSE"
    assert "FORGED_TRUE" not in result.content


def test_retrieval_marker_cannot_exceed_caller_budget():
    session, candidate, _, _ = fixture()
    with pytest.raises(FocusedContextBudgetExceeded, match="marker exceeds budget"):
        project_focused_context_retrieval(
            session, retrieval_ref=f"candidate:{candidate.candidate_id}:relations",
            max_characters=1,
        )


def test_token_measurement_without_total_limit_and_memory_revision_are_supported():
    session, candidate, _, _ = fixture()
    session.working_memory.revision = 7
    result = project_focused_decision_context(
        session, active_candidate_ids=(candidate.candidate_id,), count_tokens=lambda _: 100,
    )
    assert result.input_tokens == 100
    revisions = json.loads(result.content)["channels"]["CANONICAL_STATE"]["revisions"]
    assert revisions["working_memory"] == 7


@pytest.mark.parametrize("invalid", [0, -1, True, 48_001])
def test_retrieval_rejects_invalid_character_limits(invalid):
    session, candidate, _, _ = fixture()
    with pytest.raises(ValueError, match="budgets"):
        project_focused_context_retrieval(
            session, retrieval_ref=f"candidate:{candidate.candidate_id}:relations",
            max_characters=invalid,
        )


def fixture():
    cat = catalog()
    session = CoreSession()
    req = requirement()
    session.investigation.requirements[req.requirement_id] = req
    registration = cat.observation_payloads.require("reference.observation.value@1")
    secret = "token=must-never-enter-context"
    envelope = registration.envelope_from_model(SamplePayload(value=secret))
    provenance = Provenance(
        source_kind="test",
        producer_id="reference.resolver@1",
        trust_class=TrustClass.OBSERVED,
        acquired_at=datetime(2026, 9, 8, tzinfo=UTC),
        content_hash=envelope.content_hash,
    )
    observation = Observation.create(
        requirement_id=req.requirement_id,
        resolver_id="reference.resolver@1",
        subject_ref=req.subject_ref,
        payload=envelope,
        provenance=provenance,
    )
    session.investigation.observations[observation.observation_id] = observation
    expectation = ExpectationRef.create(
        expectation_type_id="reference.expectation.value@1",
        payload=cat.expectation_types.require(
            "reference.expectation.value@1"
        ).envelope_from_model(SamplePayload(value="normative-secret")),
        provenance=Provenance(
            source_kind="policy",
            producer_id="reference.policy@1",
            trust_class=TrustClass.DECLARED_TRUSTED,
            acquired_at=datetime(2026, 9, 8, tzinfo=UTC),
            content_hash=cat.expectation_types.require("reference.expectation.value@1")
            .envelope_from_model(SamplePayload(value="normative-secret"))
            .content_hash,
        ),
    )
    session.expectations.expectations[expectation.expectation_id] = expectation
    relations = []
    for label, verdict in (
        ("support", TriState.TRUE),
        ("contradiction", TriState.FALSE),
        ("guard", TriState.UNKNOWN),
    ):
        relation = Relation.create(
            source_ref=f"source:{label}",
            relation_type_id=RELATION_TYPE,
            target_ref=f"target:{label}",
            verdict=verdict,
            requirement_ids=[req.requirement_id],
            evidence_ids=[f"ev_{len(relations):016x}"],
            missing_reason_ids=["miss_guard"] if label == "guard" else [],
        )
        session.semantic.relations[relation.relation_id] = relation
        relations.append(relation)
    contract = ClaimContract.create(
        claim_type_id=CANDIDATE_TYPE,
        title="Focused context candidate",
        max_claim_status=ClaimStatus.SUPPORTED,
    )
    candidate = SecurityCandidate.create(
        candidate_type_id=CANDIDATE_TYPE,
        semantic_key="focused-one",
        root_cause=RootCauseRef(owner_bundle_id="reference", family_id="focus", key="one"),
        claim_contract=contract,
        max_claim_status=ClaimStatus.SUPPORTED,
        supporting_relation_ids=[relations[0].relation_id],
        contradicting_relation_ids=[relations[1].relation_id],
        guard_relation_ids=[relations[2].relation_id],
        requirement_ids=[req.requirement_id],
        expectation_ids=[expectation.expectation_id],
    )
    session.candidate_state.candidates[candidate.candidate_id] = candidate
    return session, candidate, req, secret


def test_complete_bundle_keeps_support_contradiction_guard_baseline_without_raw_payload():
    session, candidate, req, secret = fixture()
    result = project_focused_decision_context(
        session,
        active_candidate_ids=(candidate.candidate_id,),
        system_policy=("trusted-system",),
        trusted_objective="inspect safely",
    )
    assert result.complete_bundles == 1 and result.incomplete_bundles == 0
    payload = json.loads(result.content)
    bundle = payload["channels"]["CANONICAL_STATE"]["candidate_bundles"][0]
    assert bundle["status"] == "COMPLETE"
    assert set(bundle["relation_roles"]) == {"supporting", "contradicting", "guards"}
    assert len(bundle["relations"]) == 3
    assert bundle["requirements"][0]["requirement_id"] == req.requirement_id
    assert len(bundle["observations"]) == len(bundle["expectations"]) == 1
    assert secret not in result.content and "normative-secret" not in result.content
    assert "canonical_payload_json" not in result.content
    assert payload["projection"]["raw_payloads_included"] is False


def test_missing_contradiction_makes_whole_bundle_incomplete_not_partially_complete():
    session, candidate, _, _ = fixture()
    missing = candidate.contradicting_relation_ids[0]
    session.semantic.relations.pop(missing)
    result = project_focused_decision_context(
        session, active_candidate_ids=(candidate.candidate_id,)
    )
    assert result.bundle_statuses[0].status is ContextBundleStatus.CONTEXT_INCOMPLETE
    assert result.bundle_statuses[0].missing_refs == (missing,)
    marker = json.loads(result.content)["channels"]["CANONICAL_STATE"]["candidate_bundles"][0]
    assert marker["status"] == "CONTEXT_INCOMPLETE"
    assert "relations" not in marker and marker["omitted_counts"]["relations"] == 2


def test_tight_budget_uses_complete_small_marker_and_preserves_every_active_candidate():
    session, candidate, _, _ = fixture()
    result = project_focused_decision_context(
        session,
        active_candidate_ids=(candidate.candidate_id,),
        policy=FocusedContextPolicy(max_characters=2_000, max_utf8_bytes=4_000),
    )
    assert result.incomplete_bundles == 1
    assert result.character_count <= 2_000 and result.utf8_bytes <= 4_000
    marker = json.loads(result.content)["channels"]["CANONICAL_STATE"]["candidate_bundles"][0]
    assert marker["candidate_id"] == candidate.candidate_id
    assert marker["retrieval_refs"]


def test_irreducible_unicode_policy_fails_utf8_budget_without_dropping_marker():
    session, candidate, _, _ = fixture()
    with pytest.raises(FocusedContextBudgetExceeded, match="markers"):
        project_focused_decision_context(
            session,
            active_candidate_ids=(candidate.candidate_id,),
            policy=FocusedContextPolicy(max_characters=4_000, max_utf8_bytes=1_000),
            system_policy=("가" * 300,),
        )


def test_token_budget_requires_adapter_and_reserves_schema_and_output():
    session, candidate, _, _ = fixture()
    policy = FocusedContextPolicy(
        max_total_tokens=256, reserved_schema_tokens=50, reserved_output_tokens=50
    )
    with pytest.raises(ValueError, match="tokenizer"):
        project_focused_decision_context(
            session, active_candidate_ids=(candidate.candidate_id,), policy=policy
        )
    result = project_focused_decision_context(
        session,
        active_candidate_ids=(candidate.candidate_id,),
        policy=policy,
        count_tokens=lambda content: 100,
    )
    assert result.budget_mode == "TOKEN_AND_CHARACTER" and result.input_tokens == 100
    with pytest.raises(FocusedContextBudgetExceeded):
        project_focused_decision_context(
            session,
            active_candidate_ids=(candidate.candidate_id,),
            policy=policy,
            count_tokens=lambda content: 200,
        )
    for invalid in (-1, True, 1.5):
        with pytest.raises(ValueError, match="nonnegative integer"):
            project_focused_decision_context(
                session,
                active_candidate_ids=(candidate.candidate_id,),
                policy=policy,
                count_tokens=lambda content, value=invalid: value,
            )


def test_unknown_duplicate_and_empty_active_portfolio_are_rejected():
    session, candidate, _, _ = fixture()
    for ids in ((), (candidate.candidate_id, candidate.candidate_id), ("cand_0000000000000000",)):
        with pytest.raises(ValueError):
            project_focused_decision_context(session, active_candidate_ids=ids)


def test_irrelevant_global_rows_do_not_enter_focused_bundle():
    session, candidate, _, _ = fixture()
    unrelated = Relation.create(
        source_ref="unrelated-secret-ref",
        relation_type_id=RELATION_TYPE,
        target_ref="never-selected",
    )
    session.semantic.relations[unrelated.relation_id] = unrelated
    content = project_focused_decision_context(
        session, active_candidate_ids=(candidate.candidate_id,)
    ).content
    assert unrelated.relation_id not in content and "unrelated-secret-ref" not in content


def test_every_active_candidate_gets_complete_bundle_or_explicit_marker():
    session, candidate, _, _ = fixture()
    other = candidate.model_copy(
        update={
            "candidate_id": "cand_0000000000000001",
            "semantic_key": "second",
            "supporting_relation_ids": ["rel_0000000000000001"],
            "contradicting_relation_ids": [],
            "guard_relation_ids": [],
            "requirement_ids": [],
            "expectation_ids": [],
        }
    )
    session.candidate_state.candidates[other.candidate_id] = other
    result = project_focused_decision_context(
        session, active_candidate_ids=(candidate.candidate_id, other.candidate_id)
    )
    assert [row.candidate_id for row in result.bundle_statuses] == [
        candidate.candidate_id,
        other.candidate_id,
    ]
    assert [row.status for row in result.bundle_statuses] == [
        ContextBundleStatus.COMPLETE,
        ContextBundleStatus.CONTEXT_INCOMPLETE,
    ]
    bundles = json.loads(result.content)["channels"]["CANONICAL_STATE"][
        "candidate_bundles"
    ]
    assert len(bundles) == 2 and bundles[1]["missing_refs"] == ["rel_0000000000000001"]
