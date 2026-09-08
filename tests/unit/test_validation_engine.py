from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from redteam_core.extension import (
    BundleKind,
    BundleManifest,
    DomainBundle,
    SchemaCategory,
    SchemaRegistration,
    ValidatorRegistration,
    compose_bundles,
)
from redteam_core.investigation import reconcile_candidate_state
from redteam_core.knowledge import CoreSession
from redteam_core.models import (
    CandidateDisposition,
    ClaimContract,
    ClaimStatus,
    ExperimentResult,
    RootCauseRef,
    SchemaModel,
    SecurityCandidate,
    ValidationVerdict,
    canonical_json,
    stable_id,
)
from redteam_core.validation import (
    ValidationDecision,
    ValidatorDescriptor,
    ValidatorRuntimeRegistry,
    run_validation,
)
from tests.helpers import FP_A

CANDIDATE_TYPE = "validation.candidate.reference@1"
VALIDATOR_PASS = "validation.validator.pass@1"
VALIDATOR_VIOLATION = "validation.validator.violation@1"
VALIDATOR_RESULT = "validation.validator.result@1"


class CandidatePayload(SchemaModel):
    value: str


def _catalog():
    return compose_bundles(
        (
            DomainBundle(
                manifest=BundleManifest(
                    bundle_id="validation",
                    namespace="validation",
                    version="1",
                    bundle_kind=BundleKind.REFERENCE,
                    implementation_fingerprint=FP_A,
                ),
                schema_registrations=(
                    SchemaRegistration(
                        category=SchemaCategory.CANDIDATE,
                        type_id=CANDIDATE_TYPE,
                        schema_version="1",
                        owner_bundle_id="validation",
                        model=CandidatePayload,
                        implementation_fingerprint=FP_A,
                    ),
                ),
                validator_registrations=(
                    ValidatorRegistration(
                        validator_id=VALIDATOR_PASS,
                        owner_bundle_id="validation",
                        implementation_fingerprint=FP_A,
                        candidate_type_ids=frozenset({CANDIDATE_TYPE}),
                    ),
                    ValidatorRegistration(
                        validator_id=VALIDATOR_VIOLATION,
                        owner_bundle_id="validation",
                        implementation_fingerprint=FP_A,
                        candidate_type_ids=frozenset({CANDIDATE_TYPE}),
                    ),
                    ValidatorRegistration(
                        validator_id=VALIDATOR_RESULT,
                        owner_bundle_id="validation",
                        implementation_fingerprint=FP_A,
                        candidate_type_ids=frozenset({CANDIDATE_TYPE}),
                        requires_experiment_result=True,
                    ),
                ),
            ),
        )
    )


def _candidate() -> SecurityCandidate:
    contract = ClaimContract.create(
        claim_type_id=CANDIDATE_TYPE,
        title="Reference validation claim",
        max_claim_status=ClaimStatus.SUPPORTED,
    )
    return SecurityCandidate.create(
        candidate_type_id=CANDIDATE_TYPE,
        semantic_key="validation:one",
        root_cause=RootCauseRef(
            owner_bundle_id="validation",
            family_id="reference",
            key="one",
        ),
        claim_contract=contract,
        max_claim_status=ClaimStatus.SUPPORTED,
        current_claim_status=ClaimStatus.SUPPORTED,
        evidence_ids=["ev_validation"],
    )


@dataclass
class StaticValidator:
    descriptor: ValidatorDescriptor
    decision: ValidationDecision
    calls: list[str]
    fail: bool = False

    def evaluate(self, context):
        self.calls.append(self.descriptor.validator_id)
        if self.fail:
            raise RuntimeError("reference failure")
        return (self.decision,)


def _runtime(*, fail_violation: bool = False):
    calls: list[str] = []
    registry = ValidatorRuntimeRegistry()
    registry.register(
        StaticValidator(
            descriptor=ValidatorDescriptor(
                validator_id=VALIDATOR_PASS,
                candidate_type_ids=frozenset({CANDIDATE_TYPE}),
            ),
            decision=ValidationDecision(
                verdict=ValidationVerdict.PASS,
                invariant_id="INV-PASS",
                reason="reference invariant passed",
            ),
            calls=calls,
        )
    )
    registry.register(
        StaticValidator(
            descriptor=ValidatorDescriptor(
                validator_id=VALIDATOR_VIOLATION,
                candidate_type_ids=frozenset({CANDIDATE_TYPE}),
            ),
            decision=ValidationDecision(
                verdict=ValidationVerdict.VIOLATION,
                invariant_id="INV-VIOLATION",
                reason="reference invariant violated",
                evidence_ids=("ev_validation",),
            ),
            calls=calls,
            fail=fail_violation,
        )
    )
    registry.register(
        StaticValidator(
            descriptor=ValidatorDescriptor(
                validator_id=VALIDATOR_RESULT,
                candidate_type_ids=frozenset({CANDIDATE_TYPE}),
                requires_experiment_result=True,
            ),
            decision=ValidationDecision(
                verdict=ValidationVerdict.PASS,
                invariant_id="INV-RESULT",
                reason="runtime result was valid",
            ),
            calls=calls,
        )
    )
    registry.freeze()
    return registry, calls


def _session() -> tuple[CoreSession, SecurityCandidate]:
    session = CoreSession()
    candidate = _candidate()
    reconcile_candidate_state(session, (candidate,))
    return session, candidate


def test_system_runs_all_static_applicable_validators_and_preserves_every_outcome():
    session, candidate = _session()
    registry, calls = _runtime()
    run = run_validation(
        session,
        candidate=candidate,
        catalog=_catalog(),
        registry=registry,
    )

    assert set(run.validator_ids) == {VALIDATOR_PASS, VALIDATOR_VIOLATION, VALIDATOR_RESULT}
    assert set(calls) == {VALIDATOR_PASS, VALIDATOR_VIOLATION}
    outcomes = [session.validation.outcomes[item] for item in run.outcome_ids]
    assert {item.verdict for item in outcomes} == {
        ValidationVerdict.PASS,
        ValidationVerdict.VIOLATION,
        ValidationVerdict.UNKNOWN,
    }
    assert len(run.finding_ids) == 1
    finding = session.validation.findings[run.finding_ids[0]]
    violation = next(item for item in outcomes if item.verdict is ValidationVerdict.VIOLATION)
    assert finding.source_outcome_ids == [violation.outcome_id]


def test_validator_exception_becomes_unknown_without_stopping_other_validators():
    session, candidate = _session()
    registry, calls = _runtime(fail_violation=True)
    run = run_validation(session, candidate=candidate, catalog=_catalog(), registry=registry)
    outcomes = [session.validation.outcomes[item] for item in run.outcome_ids]
    error = next(item for item in outcomes if item.validator_id == VALIDATOR_VIOLATION)
    assert error.verdict is ValidationVerdict.UNKNOWN
    assert "RuntimeError" in error.reason
    assert VALIDATOR_PASS in calls


def test_exact_validation_snapshot_is_idempotent():
    session, candidate = _session()
    registry, _ = _runtime()
    first = run_validation(session, candidate=candidate, catalog=_catalog(), registry=registry)
    revision = session.validation.revision
    second = run_validation(session, candidate=candidate, catalog=_catalog(), registry=registry)
    assert first == second
    assert session.validation.revision == revision


def test_runtime_validator_set_must_match_frozen_manifest():
    session, candidate = _session()
    registry = ValidatorRuntimeRegistry()
    registry.freeze()
    with pytest.raises(ValueError, match="does not match"):
        run_validation(session, candidate=candidate, catalog=_catalog(), registry=registry)


def test_runtime_applicability_must_match_manifest():
    session, candidate = _session()
    registry, _ = _runtime()
    runtime = registry.require(VALIDATOR_PASS)
    runtime.descriptor = ValidatorDescriptor(validator_id=VALIDATOR_PASS)  # type: ignore[misc]
    with pytest.raises(ValueError, match="applicability"):
        run_validation(session, candidate=candidate, catalog=_catalog(), registry=registry)


def test_validator_invalid_evidence_grounding_fails_closed_to_unknown():
    session, candidate = _session()
    registry, _ = _runtime()
    runtime = registry.require(VALIDATOR_VIOLATION)
    runtime.decision = ValidationDecision(  # type: ignore[attr-defined]
        verdict=ValidationVerdict.VIOLATION,
        invariant_id="INV-FORGED",
        reason="attempted unsupported evidence grounding",
        evidence_ids=("ev_forged",),
    )
    run = run_validation(session, candidate=candidate, catalog=_catalog(), registry=registry)
    outcome = next(
        session.validation.outcomes[item]
        for item in run.outcome_ids
        if session.validation.outcomes[item].validator_id == VALIDATOR_VIOLATION
    )
    assert outcome.verdict is ValidationVerdict.UNKNOWN
    assert "outside canonical validation context" in outcome.reason


def test_validator_receives_deep_snapshot_not_authoritative_session():
    session, candidate = _session()
    canonical_candidate = session.candidate_state.candidates[candidate.candidate_id]
    registry, _ = _runtime()
    runtime = registry.require(VALIDATOR_PASS)

    def mutating_evaluate(context):
        context.session_snapshot.knowledge.revision = 999
        context.candidate.disposition = CandidateDisposition.REJECTED
        return (
            ValidationDecision(
                verdict=ValidationVerdict.PASS,
                invariant_id="INV-SNAPSHOT",
                reason="snapshot mutation must stay isolated",
            ),
        )

    runtime.evaluate = mutating_evaluate  # type: ignore[method-assign]
    run_validation(
        session,
        candidate=canonical_candidate,
        catalog=_catalog(),
        registry=registry,
    )
    assert session.knowledge.revision == 0
    assert (
        session.candidate_state.candidates[candidate.candidate_id].disposition
        is CandidateDisposition.UNREVIEWED
    )


def test_validation_rejects_forged_noncanonical_experiment_result():
    session, candidate = _session()
    attempt_id = "attempt_0000000000000000"
    forged = ExperimentResult(
        result_id=stable_id(
            "result",
            attempt_id,
            True,
            "",
            canonical_json(["ev_forged"]),
        ),
        experiment_id="experiment_0000000000000000",
        attempt_id=attempt_id,
        succeeded=True,
        evidence_ids=("ev_forged",),
        completed_at=datetime(2026, 9, 8, tzinfo=UTC),
    )
    registry, _ = _runtime()
    with pytest.raises(ValueError, match="not current canonical execution data"):
        run_validation(
            session,
            candidate=candidate,
            catalog=_catalog(),
            registry=registry,
            experiment_result=forged,
        )
