from __future__ import annotations

from pydantic import Field, model_validator

from redteam_core.models.base import SchemaModel, canonical_json, stable_id
from redteam_core.models.security import Finding, ValidationOutcome, ValidationVerdict


class ValidationDecision(SchemaModel):
    verdict: ValidationVerdict
    invariant_id: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=1_200)
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=128)


class ValidationRunRecord(SchemaModel):
    run_id: str = Field(pattern=r"^validation_[0-9a-f]{16}$")
    candidate_id: str = Field(pattern=r"^cand_[0-9a-f]{16}$")
    experiment_result_id: str | None = Field(default=None, pattern=r"^result_[0-9a-f]{16}$")
    knowledge_revision: int = Field(ge=0)
    expectation_revision: int = Field(ge=0)
    candidate_revision: int = Field(ge=0)
    semantic_manifest_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    validator_ids: tuple[str, ...] = ()
    outcome_ids: tuple[str, ...] = ()
    finding_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_identity(self) -> ValidationRunRecord:
        expected = stable_id(
            "validation",
            self.candidate_id,
            self.experiment_result_id or "",
            self.knowledge_revision,
            self.expectation_revision,
            self.candidate_revision,
            self.semantic_manifest_hash,
            canonical_json(sorted(self.validator_ids)),
        )
        if self.run_id != expected:
            raise ValueError("validation run id does not match snapshot identity")
        return self


class ValidationState(SchemaModel):
    revision: int = Field(default=0, ge=0)
    runs: dict[str, ValidationRunRecord] = Field(default_factory=dict)
    outcomes: dict[str, ValidationOutcome] = Field(default_factory=dict)
    findings: dict[str, Finding] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_ledgers(self) -> ValidationState:
        if any(key != item.run_id for key, item in self.runs.items()):
            raise ValueError("validation run ledger key mismatch")
        if any(key != item.outcome_id for key, item in self.outcomes.items()):
            raise ValueError("validation outcome ledger key mismatch")
        if any(key != item.finding_id for key, item in self.findings.items()):
            raise ValueError("finding ledger key mismatch")
        for run in self.runs.values():
            if set(run.outcome_ids) - set(self.outcomes):
                raise ValueError("validation run references unknown outcome")
            if set(run.finding_ids) - set(self.findings):
                raise ValueError("validation run references unknown finding")
        for finding in self.findings.values():
            if set(finding.source_outcome_ids) - set(self.outcomes):
                raise ValueError("finding references unknown validation outcome")
        return self
