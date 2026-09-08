"""Typed advisory priority state, separate from canonical truth and permissions."""

from typing import Annotated, Literal

from pydantic import ConfigDict, Field, model_validator

from redteam_core.models.base import SchemaModel, canonical_json, sha256_text
from redteam_core.models.security import CandidateDisposition


class PriorityProviderDescriptor(SchemaModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    provider_id: str = Field(min_length=1, max_length=160)
    implementation_version: str = Field(min_length=1, max_length=80)
    facts_policy_id: Literal["core.priority.canonical-facts@3"] = "core.priority.canonical-facts@3"
    floor_slots: int = Field(default=1, ge=0, le=64)
    max_wait_batches: int = Field(default=256, ge=1, le=4096)
    max_roots: int = Field(default=256, ge=1, le=256)
    max_candidates: int = Field(default=4096, ge=1, le=4096)

    @property
    def binding_id(self) -> str:
        return (
            self.provider_id
            + ":sha256:"
            + sha256_text(canonical_json(self.model_dump(mode="json")))
        )


class PriorityFacts(SchemaModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    candidate_id: str = Field(pattern=r"^cand_[0-9a-f]{16}$")
    root_key: str = Field(pattern=r"^root_[0-9a-f]{16}$")
    family: str = Field(min_length=1, max_length=400)
    baseline_total: int
    resolvable_count: int = Field(ge=0)
    shared_unresolved_roots: int = Field(ge=0)
    fresh_count: int = Field(ge=0)
    conflict_count: int = Field(ge=0)
    cost_bucket: int = Field(ge=0, le=5)
    failure_count: int = Field(ge=0)
    age_batches: int = Field(ge=0)


class PriorityComponent(SchemaModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1, max_length=80)
    value: int = Field(ge=-1_000_000, le=1_000_000)


class PriorityScore(SchemaModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    candidate_id: str = Field(pattern=r"^cand_[0-9a-f]{16}$")
    components: tuple[PriorityComponent, ...] = Field(min_length=1, max_length=16)
    reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def unique_components(self):
        if len({item.name for item in self.components}) != len(self.components):
            raise ValueError("duplicate priority score component")
        return self

    @property
    def total(self) -> int:
        return sum(item.value for item in self.components)


class PriorityTerminalBinding(SchemaModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    disposition: CandidateDisposition
    evidence_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    user_final: bool = False

    @model_validator(mode="after")
    def terminal_only(self):
        if self.disposition in {
            CandidateDisposition.UNREVIEWED,
            CandidateDisposition.INVESTIGATING,
        }:
            raise ValueError("priority terminal binding requires terminal disposition")
        return self

    @property
    def locked(self) -> bool:
        return self.user_final or self.disposition is CandidateDisposition.MERGED


class RootPriorityState(SchemaModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    root_key: str = Field(pattern=r"^root_[0-9a-f]{16}$")
    evidence_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    first_seen_batch: int = Field(ge=0)
    last_selected_batch: int = Field(default=0, ge=0)
    selection_count: int = Field(default=0, ge=0)
    seen_failure_ids: tuple[Annotated[str, Field(pattern=r"^fail_[0-9a-f]{16}$")], ...] = Field(
        default=(), max_length=4096
    )
    active: bool = True
    terminal: PriorityTerminalBinding | None = None
    reevaluation_required: bool = False

    @model_validator(mode="after")
    def validate_binding(self):
        if tuple(sorted(set(self.seen_failure_ids))) != self.seen_failure_ids:
            raise ValueError("priority failure IDs must be unique and sorted")
        expected = self.terminal is not None and self.terminal.evidence_hash != self.evidence_hash
        if self.reevaluation_required != expected:
            raise ValueError("priority reassessment flag disagrees with terminal evidence binding")
        return self


class PriorityAssessment(SchemaModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    facts: PriorityFacts
    score: PriorityScore
    evidence_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    eligible: bool
    blocked_reasons: tuple[Annotated[str, Field(min_length=1, max_length=100)], ...] = Field(
        default=(), max_length=128
    )
    reevaluation_required: bool = False

    @model_validator(mode="after")
    def validate_candidate_binding(self):
        if self.facts.candidate_id != self.score.candidate_id:
            raise ValueError("priority score candidate binding mismatch")
        if not self.eligible and not self.blocked_reasons:
            raise ValueError("ineligible priority assessment requires an explicit reason")
        return self


class PrioritySelection(SchemaModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    batch_index: int = Field(ge=1)
    provider_binding: str = Field(min_length=1, max_length=300)
    input_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    expected_priority_revision: int = Field(ge=0)
    assessments: tuple[PriorityAssessment, ...] = Field(default=(), max_length=256)
    selected_candidate_ids: tuple[str, ...] = Field(default=(), max_length=64)
    fairness_bound_batches: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_selection(self):
        roots = [row.facts.root_key for row in self.assessments]
        ids = [row.facts.candidate_id for row in self.assessments]
        if len(set(roots)) != len(roots) or len(set(ids)) != len(ids):
            raise ValueError("priority assessment must have unique roots and representatives")
        eligible = {row.facts.candidate_id for row in self.assessments if row.eligible}
        if len(set(self.selected_candidate_ids)) != len(self.selected_candidate_ids):
            raise ValueError("duplicate priority selection")
        if not set(self.selected_candidate_ids) <= eligible:
            raise ValueError("priority selection references an ineligible candidate")
        return self


class PriorityDecisionRecord(SchemaModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    root_key: str = Field(pattern=r"^root_[0-9a-f]{16}$")
    batch_index: int = Field(ge=1)
    binding: PriorityTerminalBinding
    reason: str = Field(min_length=1, max_length=1000)


class PriorityState(SchemaModel):
    revision: int = Field(default=0, ge=0)
    batch_sequence: int = Field(default=0, ge=0, le=256)
    roots: dict[str, RootPriorityState] = Field(default_factory=dict, max_length=4096)
    selections: tuple[PrioritySelection, ...] = Field(default=(), max_length=256)
    decisions: tuple[PriorityDecisionRecord, ...] = Field(default=(), max_length=4096)

    @model_validator(mode="after")
    def validate_state(self):
        decision_keys = [(row.batch_index, row.root_key) for row in self.decisions]
        if len(set(decision_keys)) != len(decision_keys):
            raise ValueError("duplicate priority decision per root/batch")
        for row in self.decisions:
            if row.root_key not in self.roots or row.batch_index > self.batch_sequence:
                raise ValueError("priority decision lacks its root/batch")
        if any(key != row.root_key for key, row in self.roots.items()):
            raise ValueError("priority root ledger key mismatch")
        if any(row.last_selected_batch > self.batch_sequence for row in self.roots.values()):
            raise ValueError("priority root selection is in the future")
        if tuple(row.batch_index for row in self.selections) != tuple(
            range(1, self.batch_sequence + 1)
        ):
            raise ValueError("priority selection history must be contiguous and complete")
        counts, last = {}, {}
        selected_roots_by_batch = {}
        for selection in self.selections:
            selected = {
                row.facts.root_key: row
                for row in selection.assessments
                if row.facts.candidate_id in selection.selected_candidate_ids
            }
            selected_roots_by_batch[selection.batch_index] = selected
            for root in selected:
                counts[root] = counts.get(root, 0) + 1
                last[root] = selection.batch_index
        if not set(counts) <= set(self.roots):
            raise ValueError("priority history references missing roots")
        for root, row in self.roots.items():
            if row.selection_count != counts.get(root, 0) or row.last_selected_batch != last.get(
                root, 0
            ):
                raise ValueError("priority aging/count does not match selection history")
        latest_decisions = {}
        for decision in self.decisions:
            selected = selected_roots_by_batch.get(decision.batch_index, {})
            if decision.root_key not in selected or decision.binding.evidence_hash != (
                selected[decision.root_key].evidence_hash
            ):
                raise ValueError("priority decision is not bound to selected root evidence")
            latest_decisions[decision.root_key] = decision.binding
        if any(self.roots[root].terminal != binding for root, binding in latest_decisions.items()):
            raise ValueError("priority terminal binding disagrees with decision history")
        if self.revision < self.batch_sequence + len(self.decisions):
            raise ValueError("priority revision is behind durable history")
        if len(self.model_dump_json().encode("utf-8")) > 16_777_216:
            raise ValueError("priority ledger byte capacity exhausted")
        return self
