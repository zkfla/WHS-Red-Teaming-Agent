from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from redteam_core.investigation.requirements import (
    EvidenceRequirement,
    Observation,
    ResolutionFailureRecord,
)
from redteam_core.models.base import SchemaModel
from redteam_core.models.execution import ExecutionState
from redteam_core.models.reasoning import AgentWorkingMemory
from redteam_core.models.security import (
    CandidateDisposition,
    ExpectationRef,
    Relation,
    SecurityCandidate,
)
from redteam_core.models.validation import ValidationState


class KnowledgeSlotStatus(StrEnum):
    UNKNOWN = "UNKNOWN"
    CONSISTENT = "CONSISTENT"
    CONFLICTING = "CONFLICTING"


class KnowledgeSlot(SchemaModel):
    requirement_id: str = Field(pattern=r"^req_[0-9a-f]{16}$")
    status: KnowledgeSlotStatus = KnowledgeSlotStatus.UNKNOWN
    effective_payload_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    authoritative_payload_hashes: tuple[str, ...] = ()
    observation_ids: tuple[str, ...] = ()
    authoritative_observation_ids: tuple[str, ...] = ()
    inferred_observation_ids: tuple[str, ...] = ()
    stale_observation_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_status(self) -> KnowledgeSlot:
        hashes = self.authoritative_payload_hashes
        if self.status is KnowledgeSlotStatus.CONSISTENT:
            if len(hashes) != 1 or self.effective_payload_hash != hashes[0]:
                raise ValueError("consistent knowledge slot requires exactly one effective hash")
        elif self.status is KnowledgeSlotStatus.CONFLICTING:
            if len(hashes) < 2 or self.effective_payload_hash is not None:
                raise ValueError("conflicting knowledge slot cannot select an effective payload")
        elif self.effective_payload_hash is not None:
            raise ValueError("unknown knowledge slot cannot have an effective payload")
        return self


class KnowledgeState(SchemaModel):
    revision: int = Field(default=0, ge=0)
    slots: dict[str, KnowledgeSlot] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_slot_keys(self) -> KnowledgeState:
        if any(key != value.requirement_id for key, value in self.slots.items()):
            raise ValueError("knowledge slot key does not match requirement_id")
        return self


class ExpectationState(SchemaModel):
    revision: int = Field(default=0, ge=0)
    expectations: dict[str, ExpectationRef] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_expectation_keys(self) -> ExpectationState:
        if any(key != value.expectation_id for key, value in self.expectations.items()):
            raise ValueError("expectation ledger key does not match expectation_id")
        return self


class InvestigationState(SchemaModel):
    requirements: dict[str, EvidenceRequirement] = Field(default_factory=dict)
    observations: dict[str, Observation] = Field(default_factory=dict)
    resolution_failures: dict[str, ResolutionFailureRecord] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_ledgers(self) -> InvestigationState:
        if any(key != value.requirement_id for key, value in self.requirements.items()):
            raise ValueError("requirement ledger key does not match requirement_id")
        if any(key != value.observation_id for key, value in self.observations.items()):
            raise ValueError("observation ledger key does not match observation_id")
        if any(key != value.failure_id for key, value in self.resolution_failures.items()):
            raise ValueError("failure ledger key does not match failure_id")
        for observation in self.observations.values():
            requirement = self.requirements.get(observation.requirement_id)
            if requirement is None:
                raise ValueError("observation references unknown requirement")
            if observation.subject_ref != requirement.subject_ref:
                raise ValueError("observation subject does not match requirement")
        return self


class SemanticState(SchemaModel):
    relations: dict[str, Relation] = Field(default_factory=dict)
    relation_producers: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_relation_ledgers(self) -> SemanticState:
        if any(key != value.relation_id for key, value in self.relations.items()):
            raise ValueError("relation ledger key does not match relation_id")
        if set(self.relation_producers) - set(self.relations):
            raise ValueError("relation producer ledger references unknown relation")
        return self


class CoverageLedgerEntry(SchemaModel):
    item_id: str = Field(pattern=r"^cand_[0-9a-f]{16}$")
    candidate_id: str = Field(pattern=r"^cand_[0-9a-f]{16}$")
    root_cause_key: str = Field(min_length=1, max_length=900)
    disposition: CandidateDisposition = CandidateDisposition.UNREVIEWED
    selection_count: int = Field(default=0, ge=0)
    last_selected_batch: int | None = Field(default=None, ge=1)
    last_seen_knowledge_revision: int = Field(default=0, ge=0)
    active: bool = True
    reason: str | None = Field(default=None, max_length=1_000)

    @model_validator(mode="after")
    def validate_identity(self) -> CoverageLedgerEntry:
        if self.item_id != self.candidate_id:
            raise ValueError("coverage item_id must equal candidate_id")
        return self


class CandidateState(SchemaModel):
    revision: int = Field(default=0, ge=0)
    candidates: dict[str, SecurityCandidate] = Field(default_factory=dict)
    coverage_ledger: dict[str, CoverageLedgerEntry] = Field(default_factory=dict)
    batch_sequence: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_ledgers(self) -> CandidateState:
        if any(key != value.candidate_id for key, value in self.candidates.items()):
            raise ValueError("candidate ledger key does not match candidate_id")
        if any(key != value.item_id for key, value in self.coverage_ledger.items()):
            raise ValueError("coverage ledger key does not match item_id")
        for candidate_id, candidate in self.candidates.items():
            ledger = self.coverage_ledger.get(candidate_id)
            if ledger is None:
                raise ValueError("active candidate has no coverage ledger entry")
            if not ledger.active:
                raise ValueError("current candidate cannot have an inactive coverage entry")
            if ledger.root_cause_key != candidate.root_cause.qualified_key:
                raise ValueError("coverage root cause does not match candidate root cause")
            if ledger.disposition is not candidate.disposition:
                raise ValueError("coverage disposition does not match candidate disposition")
        for candidate_id, ledger in self.coverage_ledger.items():
            if ledger.active and candidate_id not in self.candidates:
                raise ValueError("active coverage entry references a missing candidate")
        return self


class CoreSession(SchemaModel):
    knowledge: KnowledgeState = Field(default_factory=KnowledgeState)
    expectations: ExpectationState = Field(default_factory=ExpectationState)
    investigation: InvestigationState = Field(default_factory=InvestigationState)
    semantic: SemanticState = Field(default_factory=SemanticState)
    candidate_state: CandidateState = Field(default_factory=CandidateState)
    working_memory: AgentWorkingMemory = Field(default_factory=AgentWorkingMemory)
    validation: ValidationState = Field(default_factory=ValidationState)
    execution: ExecutionState = Field(default_factory=ExecutionState)


class CommitResult(SchemaModel):
    accepted: bool
    recorded_observation_ids: tuple[str, ...] = ()
    duplicate_observation_ids: tuple[str, ...] = ()
    affected_requirement_ids: tuple[str, ...] = ()
    recomputed_relation_ids: tuple[str, ...] = ()
    semantic_changed: bool = False
    revision: int = Field(ge=0)
