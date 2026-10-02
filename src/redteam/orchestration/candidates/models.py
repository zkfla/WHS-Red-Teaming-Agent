"""Typed candidate and priority models.

Domain providers produce normalized signals with source references.  The
scheduler never receives finance-specific amounts, account state, or similar
domain objects.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator

from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.ids import AgentId, GraphNodeId
from redteam.orchestration.agents.base import PlanStep


class PrioritySignals(BaseModel):
    expected_information_gain: float = Field(default=0.5, ge=0.0, le=1.0)
    objective_progress: float = Field(default=0.5, ge=0.0, le=1.0)
    evidence_gap_reduction: float = Field(default=0.0, ge=0.0, le=1.0)
    downstream_unblock_count: int = Field(default=0, ge=0)
    historical_success: float = Field(default=0.5, ge=0.0, le=1.0)
    estimated_difficulty: float = Field(default=0.5, ge=0.0, le=1.0)
    expected_cost: float = Field(default=0.5, ge=0.0, le=1.0)
    operational_risk: float = Field(default=0.0, ge=0.0, le=1.0)
    repetition_penalty: float = Field(default=0.0, ge=0.0, le=1.0)
    impact: float = Field(default=0.0, ge=0.0, le=1.0)
    source_refs: list[str] = Field(default_factory=list)
    producer_version: str = "1"

    @field_validator("source_refs")
    @classmethod
    def _normalise_source_refs(cls, value: list[str]) -> list[str]:
        return sorted({item.strip() for item in value if item.strip()})

    @model_validator(mode="after")
    def _custom_scores_need_provenance(self) -> PrioritySignals:
        customised = any(
            (
                self.expected_information_gain != 0.5,
                self.objective_progress != 0.5,
                self.evidence_gap_reduction != 0.0,
                self.downstream_unblock_count != 0,
                self.historical_success != 0.5,
                self.estimated_difficulty != 0.5,
                self.expected_cost != 0.5,
                self.operational_risk != 0.0,
                self.repetition_penalty != 0.0,
                self.impact != 0.0,
            )
        )
        if customised and not self.source_refs:
            raise ValueError("custom priority signals require at least one source_ref")
        if not self.producer_version.strip():
            raise ValueError("priority signal producer_version must not be blank")
        return self


class PriorityScore(BaseModel):
    total: float
    components: dict[str, float] = Field(default_factory=dict)


class CandidateDraft(BaseModel):
    contract_version: str = "1"
    agent_id: AgentId
    plan_step: PlanStep
    target_node_ids: list[GraphNodeId]
    prerequisite_fingerprint: str = ""
    execution_identity_version: int = Field(default=0, ge=0)
    prerequisite_ids: list[str] = Field(default_factory=list)
    conflict_keys: list[str] = Field(default_factory=list)
    signals: PrioritySignals = Field(default_factory=PrioritySignals)

    @field_validator("target_node_ids")
    @classmethod
    def _needs_a_target(cls, value: list[GraphNodeId]) -> list[GraphNodeId]:
        if not value:
            raise ValueError("a candidate needs at least one target node id")
        return sorted(set(value), key=str)

    @field_validator("conflict_keys")
    @classmethod
    def _normalise_conflicts(cls, value: list[str]) -> list[str]:
        cleaned = sorted({item.strip() for item in value if item.strip()})
        if any(any(ch < " " for ch in item) for item in cleaned):
            raise ValueError("conflict keys may not contain control characters")
        return cleaned


class ExecutableCandidate(BaseModel):
    candidate_key: str
    draft: CandidateDraft
    tool: ToolDescriptor
    priority: PriorityScore

    @property
    def plan_step(self) -> PlanStep:
        return self.draft.plan_step.model_copy(update={"tool_id": self.tool.tool_id})
