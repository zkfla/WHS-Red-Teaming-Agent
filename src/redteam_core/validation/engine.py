from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from redteam_core.extension.bundle import ExtensionCatalog
from redteam_core.knowledge.state import CoreSession
from redteam_core.models.base import canonical_json, stable_id
from redteam_core.models.execution import ExperimentResult
from redteam_core.models.security import (
    ClaimStatus,
    Finding,
    SecurityCandidate,
    ValidationOutcome,
    ValidationVerdict,
)
from redteam_core.models.validation import ValidationDecision, ValidationRunRecord


@dataclass(frozen=True)
class ValidatorDescriptor:
    validator_id: str
    candidate_type_ids: frozenset[str] = frozenset()
    requires_experiment_result: bool = False


@dataclass(frozen=True)
class ValidationContext:
    session_snapshot: CoreSession
    candidate: SecurityCandidate
    experiment_result: ExperimentResult | None = None


class Validator(Protocol):
    descriptor: ValidatorDescriptor

    def evaluate(self, context: ValidationContext) -> tuple[ValidationDecision, ...]: ...


class ValidatorRuntimeRegistry:
    def __init__(self) -> None:
        self._items: dict[str, Validator] = {}
        self._frozen = False

    @property
    def frozen(self) -> bool:
        return self._frozen

    def register(self, validator: Validator) -> None:
        if self._frozen:
            raise RuntimeError("validator runtime registry is frozen")
        key = validator.descriptor.validator_id
        if key in self._items:
            raise ValueError(f"duplicate validator runtime: {key}")
        self._items[key] = validator

    def freeze(self) -> None:
        self._frozen = True

    def require(self, validator_id: str) -> Validator:
        try:
            return self._items[validator_id]
        except KeyError as exc:
            raise KeyError(f"unregistered validator runtime: {validator_id}") from exc

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._items))


def _validate_runtime_manifest(
    *, catalog: ExtensionCatalog, registry: ValidatorRuntimeRegistry
) -> None:
    if not catalog.frozen or not registry.frozen:
        raise RuntimeError("validator catalog and runtime registry must be frozen")
    registered = tuple(item.validator_id for item in catalog.validators.values())
    if registry.ids() != registered:
        raise ValueError("runtime validator set does not match frozen semantic manifest")
    for registration in catalog.validators.values():
        runtime = registry.require(registration.validator_id)
        descriptor = runtime.descriptor
        if descriptor.candidate_type_ids != registration.candidate_type_ids:
            raise ValueError("runtime validator candidate applicability differs from manifest")
        if descriptor.requires_experiment_result != registration.requires_experiment_result:
            raise ValueError("runtime validator result applicability differs from manifest")


def _materialize_findings(
    candidate: SecurityCandidate,
    outcomes: tuple[ValidationOutcome, ...],
) -> tuple[Finding, ...]:
    if candidate.disposition.value in {"REJECTED", "MERGED"}:
        return ()
    findings: list[Finding] = []
    for outcome in outcomes:
        if outcome.verdict not in {ValidationVerdict.VIOLATION, ValidationVerdict.ANOMALY}:
            continue
        status = (
            candidate.max_claim_status
            if outcome.verdict is ValidationVerdict.VIOLATION
            else ClaimStatus.CANDIDATE
        )
        findings.append(
            Finding.create(
                finding_type_id=candidate.claim_contract.claim_type_id,
                candidate_id=candidate.candidate_id,
                source_outcome_ids=[outcome.outcome_id],
                status=status,
                title=candidate.claim_contract.title,
                summary=outcome.reason,
                evidence_ids=sorted(set(candidate.evidence_ids) | set(outcome.evidence_ids)),
            )
        )
    return tuple(sorted(findings, key=lambda item: item.finding_id))


def run_validation(
    session: CoreSession,
    *,
    candidate: SecurityCandidate,
    catalog: ExtensionCatalog,
    registry: ValidatorRuntimeRegistry,
    experiment_result: ExperimentResult | None = None,
) -> ValidationRunRecord:
    _validate_runtime_manifest(catalog=catalog, registry=registry)
    canonical_candidate = session.candidate_state.candidates.get(candidate.candidate_id)
    if canonical_candidate is None or canonical_candidate != candidate:
        raise ValueError("validation candidate is not the current canonical candidate")
    canonical_result: ExperimentResult | None = None
    if experiment_result is not None:
        canonical_result = session.execution.results.get(experiment_result.result_id)
        if canonical_result is None or canonical_result != experiment_result:
            raise ValueError("validation experiment result is not current canonical execution data")

    applicable = tuple(
        registration
        for registration in catalog.validators.values()
        if not registration.candidate_type_ids
        or candidate.candidate_type_id in registration.candidate_type_ids
    )
    validator_ids = tuple(item.validator_id for item in applicable)
    run_id = stable_id(
        "validation",
        candidate.candidate_id,
        experiment_result.result_id if experiment_result else "",
        session.knowledge.revision,
        session.expectations.revision,
        session.candidate_state.revision,
        catalog.semantic_manifest_hash,
        canonical_json(sorted(validator_ids)),
    )
    existing = session.validation.runs.get(run_id)
    if existing is not None:
        return existing

    session_snapshot = session.model_copy(deep=True)
    snapshot_candidate = session_snapshot.candidate_state.candidates[candidate.candidate_id]
    snapshot_result = (
        session_snapshot.execution.results[canonical_result.result_id]
        if canonical_result is not None
        else None
    )
    context = ValidationContext(
        session_snapshot=session_snapshot,
        candidate=snapshot_candidate,
        experiment_result=snapshot_result,
    )
    allowed_evidence_ids = set(candidate.evidence_ids)
    for grounding_ref in candidate.grounding_refs:
        expectation = session.expectations.expectations.get(grounding_ref)
        if expectation is not None:
            allowed_evidence_ids.update(expectation.evidence_ids)
    for relation_id in (
        *candidate.supporting_relation_ids,
        *candidate.contradicting_relation_ids,
        *candidate.guard_relation_ids,
    ):
        relation = session.semantic.relations.get(relation_id)
        if relation is not None:
            allowed_evidence_ids.update(relation.evidence_ids)
    if experiment_result is not None:
        allowed_evidence_ids.update(experiment_result.evidence_ids)
    outcomes: list[ValidationOutcome] = []
    for registration in applicable:
        decisions: tuple[ValidationDecision, ...]
        if registration.requires_experiment_result and experiment_result is None:
            decisions = (
                ValidationDecision(
                    verdict=ValidationVerdict.UNKNOWN,
                    invariant_id=f"{registration.validator_id}:required-result",
                    reason="required experiment result is unavailable",
                ),
            )
        else:
            validator = registry.require(registration.validator_id)
            try:
                decisions = validator.evaluate(context)
            except Exception as exc:  # fail closed while preserving the validator's audit slot
                decisions = (
                    ValidationDecision(
                        verdict=ValidationVerdict.UNKNOWN,
                        invariant_id=f"{registration.validator_id}:execution-error",
                        reason=f"validator raised {type(exc).__name__}",
                    ),
                )
            if not decisions:
                decisions = (
                    ValidationDecision(
                        verdict=ValidationVerdict.UNKNOWN,
                        invariant_id=f"{registration.validator_id}:no-outcome",
                        reason="validator returned no outcome",
                    ),
                )
        for decision in decisions:
            outside_evidence = sorted(set(decision.evidence_ids) - allowed_evidence_ids)
            if outside_evidence:
                decision = ValidationDecision(
                    verdict=ValidationVerdict.UNKNOWN,
                    invariant_id=f"{registration.validator_id}:invalid-grounding",
                    reason="validator referenced evidence outside canonical validation context",
                )
            outcomes.append(
                ValidationOutcome.create(
                    validator_id=registration.validator_id,
                    verdict=decision.verdict,
                    invariant_id=decision.invariant_id,
                    reason=decision.reason,
                    candidate_id=candidate.candidate_id,
                    experiment_result_id=(
                        experiment_result.result_id if experiment_result is not None else None
                    ),
                    knowledge_revision=session.knowledge.revision,
                    candidate_revision=session.candidate_state.revision,
                    evidence_ids=list(decision.evidence_ids),
                )
            )

    ordered_outcomes = tuple(sorted(outcomes, key=lambda item: item.outcome_id))
    findings = _materialize_findings(candidate, ordered_outcomes)
    candidate_state = session.validation.model_copy(deep=True)
    for outcome in ordered_outcomes:
        existing_outcome = candidate_state.outcomes.get(outcome.outcome_id)
        if existing_outcome is not None and existing_outcome != outcome:
            raise ValueError("validation outcome ID collision")
        candidate_state.outcomes[outcome.outcome_id] = outcome
    for finding in findings:
        existing_finding = candidate_state.findings.get(finding.finding_id)
        if existing_finding is not None and existing_finding != finding:
            raise ValueError("finding ID collision")
        candidate_state.findings[finding.finding_id] = finding
    run = ValidationRunRecord(
        run_id=run_id,
        candidate_id=candidate.candidate_id,
        experiment_result_id=experiment_result.result_id if experiment_result else None,
        knowledge_revision=session.knowledge.revision,
        expectation_revision=session.expectations.revision,
        candidate_revision=session.candidate_state.revision,
        semantic_manifest_hash=catalog.semantic_manifest_hash,
        validator_ids=validator_ids,
        outcome_ids=tuple(item.outcome_id for item in ordered_outcomes),
        finding_ids=tuple(item.finding_id for item in findings),
    )
    candidate_state.runs[run.run_id] = run
    candidate_state.revision += 1
    session.validation = candidate_state
    return run
