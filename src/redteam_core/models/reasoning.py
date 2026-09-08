from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from redteam_core.models.base import SchemaModel, stable_id


class WorkingHypothesisStatus(StrEnum):
    ACTIVE = "ACTIVE"
    BLOCKED = "BLOCKED"
    READY_FOR_PROPOSAL = "READY_FOR_PROPOSAL"
    DISCARDED = "DISCARDED"


class HypothesisNextCheck(SchemaModel):
    objective: str = Field(min_length=1, max_length=300)
    target_ref: str | None = Field(default=None, max_length=500)


class WorkingHypothesis(SchemaModel):
    hypothesis_id: str = Field(pattern=r"^hyp_[0-9a-f]{16}$")
    semantic_key: str = Field(min_length=1, max_length=500)
    candidate_id: str | None = Field(default=None, pattern=r"^cand_[0-9a-f]{16}$")
    claim: str = Field(min_length=1, max_length=300)
    status: WorkingHypothesisStatus = WorkingHypothesisStatus.ACTIVE
    supporting_relation_ids: tuple[str, ...] = Field(default=(), max_length=16)
    contradicting_relation_ids: tuple[str, ...] = Field(default=(), max_length=16)
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=24)
    missing_reason_ids: tuple[str, ...] = Field(default=(), max_length=16)
    requirement_ids: tuple[str, ...] = Field(default=(), max_length=16)
    grounding_refs: tuple[str, ...] = Field(default=(), max_length=32)
    decision_summary: str = Field(min_length=1, max_length=600)
    next_check: HypothesisNextCheck | None = None
    created_revision: int = Field(ge=1)
    updated_revision: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_identity_and_grounding(self) -> WorkingHypothesis:
        expected = stable_id("hyp", self.semantic_key)
        if self.hypothesis_id != expected:
            raise ValueError("hypothesis_id does not match semantic_key")
        if set(self.supporting_relation_ids) & set(self.contradicting_relation_ids):
            raise ValueError("a relation cannot both support and contradict one hypothesis")
        if self.status is not WorkingHypothesisStatus.DISCARDED and not (
            self.candidate_id or self.supporting_relation_ids or self.contradicting_relation_ids
        ):
            raise ValueError("non-discarded hypothesis requires canonical grounding")
        if self.status is WorkingHypothesisStatus.BLOCKED and not (
            self.missing_reason_ids or self.requirement_ids or self.next_check
        ):
            raise ValueError("blocked hypothesis requires unresolved work")
        if self.status is WorkingHypothesisStatus.DISCARDED and self.next_check is not None:
            raise ValueError("discarded hypothesis cannot retain next_check")
        return self


class AgentWorkingMemory(SchemaModel):
    revision: int = Field(default=0, ge=0)
    focus_hypothesis_id: str | None = Field(default=None, pattern=r"^hyp_[0-9a-f]{16}$")
    hypotheses: dict[str, WorkingHypothesis] = Field(default_factory=dict, max_length=8)
    last_reconciled_knowledge_revision: int = Field(default=0, ge=0)
    last_reconciled_candidate_revision: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_hypothesis_ledger(self) -> AgentWorkingMemory:
        if any(key != item.hypothesis_id for key, item in self.hypotheses.items()):
            raise ValueError("working-memory ledger key does not match hypothesis_id")
        if self.focus_hypothesis_id is not None and self.focus_hypothesis_id not in self.hypotheses:
            raise ValueError("working-memory focus references unknown hypothesis")
        return self


class WorkingHypothesisUpsert(SchemaModel):
    hypothesis_id: str | None = Field(default=None, pattern=r"^hyp_[0-9a-f]{16}$")
    semantic_key: str | None = Field(default=None, min_length=1, max_length=500)
    candidate_id: str | None = Field(default=None, pattern=r"^cand_[0-9a-f]{16}$")
    claim: str | None = Field(default=None, min_length=1, max_length=300)
    status: WorkingHypothesisStatus | None = None
    supporting_relation_ids: tuple[str, ...] | None = Field(default=None, max_length=16)
    contradicting_relation_ids: tuple[str, ...] | None = Field(default=None, max_length=16)
    decision_summary: str | None = Field(default=None, min_length=1, max_length=600)
    next_check: HypothesisNextCheck | None = None
    clear_next_check: bool = False


class WorkingMemoryPatch(SchemaModel):
    expected_revision: int = Field(ge=0)
    upserts: tuple[WorkingHypothesisUpsert, ...] = Field(default=(), max_length=3)
    focus_hypothesis_id: str | None = Field(default=None, pattern=r"^hyp_[0-9a-f]{16}$")
    clear_focus: bool = False


class WorkingMemoryUpdateResult(SchemaModel):
    accepted: bool
    changed: bool = False
    revision: int = Field(ge=0)
    updated_hypothesis_ids: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


class HypothesisReconciliationResult(SchemaModel):
    changed: bool
    revision: int = Field(ge=0)
    ready_hypothesis_ids: tuple[str, ...] = ()
    blocked_hypothesis_ids: tuple[str, ...] = ()
    discarded_hypothesis_ids: tuple[str, ...] = ()
