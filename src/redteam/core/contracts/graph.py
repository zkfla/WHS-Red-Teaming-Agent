"""Shared Attack Graph domain model: nodes, edges, hypotheses, findings.

Owned by PM (schema) — Agent Team owns the store/service in
``redteam.attack_graph``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator
from pydantic_core import PydanticCustomError

from redteam.core.enums import GraphEdgeType, GraphNodeType
from redteam.core.ids import GraphEdgeId, GraphNodeId
from redteam.core.redaction import redact_sensitive_text


class GraphNode(BaseModel):
    id: GraphNodeId
    type: GraphNodeType
    labels: list[str] = Field(default_factory=list)
    properties: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class GraphEdge(BaseModel):
    id: GraphEdgeId
    type: GraphEdgeType
    source_id: GraphNodeId
    target_id: GraphNodeId
    properties: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class Hypothesis(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    hypothesis_id: str
    description: str
    confidence: float = 0.0
    status: str = "open"
    metadata: dict[str, Any] = Field(default_factory=dict)
    # v1 remains readable without changing its serialized identity.
    contract_version: Literal[1, 2] = 1
    revision: int = Field(default=0, ge=0, strict=True)
    target_node_ids: list[str] = Field(default_factory=list)
    principal_refs: list[str] = Field(default_factory=list)
    premise: str | None = None
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    refuting_evidence_ids: list[str] = Field(default_factory=list)
    source_hypothesis_ids: list[str] = Field(default_factory=list)
    reason: str | None = None

    @model_validator(mode="after")
    def _candidate_contract(self):
        if self.contract_version == 2:
            if self.status not in {"open", "confirmed", "rejected", "inconclusive", "held"}:
                raise ValueError("unsupported candidate status; refuted maps to rejected")
            if not self.hypothesis_id.strip() or not self.description.strip():
                raise ValueError("candidate identity and description must be nonblank")
            if not self.target_node_ids or not self.reason or not self.reason.strip():
                raise ValueError("candidate needs registered targets and a judgment reason")
            if not 0 <= self.confidence <= 1:
                raise ValueError("candidate confidence must be between zero and one")
            for name in ("description", "premise", "reason"):
                value = getattr(self, name)
                if value is not None:
                    setattr(self, name, redact_sensitive_text(value.strip()))
            for name in ("target_node_ids", "principal_refs", "supporting_evidence_ids",
                         "refuting_evidence_ids", "source_hypothesis_ids"):
                values = getattr(self, name)
                if any(not value.strip() for value in values):
                    raise ValueError("candidate references must be nonblank")
                setattr(self, name, sorted(set(values)))
            if self.hypothesis_id in self.source_hypothesis_ids:
                raise ValueError("candidate cannot be its own source")
        return self

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        result = handler(self)
        if self.contract_version == 1:
            for key in ("contract_version", "revision", "target_node_ids", "principal_refs",
                        "premise", "supporting_evidence_ids", "refuting_evidence_ids",
                        "source_hypothesis_ids", "reason"):
                result.pop(key, None)
        return result


class HypothesisUpdate(BaseModel):
    """Code-bound candidate proposal, consumed by atomic graph/Task admission.

    New IDs are issued by the caller's code, not accepted as model authority.
    None expects absence; zero explicitly upgrades an existing v1 hypothesis.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    hypothesis: Hypothesis
    expected_revision: int | None = Field(default=None, ge=0, strict=True)
    review_evidence_id: str | None = None

    @model_validator(mode="after")
    def _version(self):
        if self.hypothesis.contract_version != 2:
            raise PydanticCustomError(
                "candidate_update_requires_v2", "candidate updates require hypothesis contract v2",
            )
        if self.hypothesis.status in {"confirmed", "rejected"} and not self.review_evidence_id:
            raise PydanticCustomError(
                "candidate_verdict_requires_review",
                "candidate verdict requires an independent review receipt",
            )
        return self


class Finding(BaseModel):
    finding_id: str
    title: str
    description: str = ""
    severity: str = "info"
    verified: bool = False
    evidence_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
