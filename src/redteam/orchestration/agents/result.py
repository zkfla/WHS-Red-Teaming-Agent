"""Typed worker hand-off models; summaries are never canonical state."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from redteam.core.ids import EvidenceId, FindingId, GraphNodeId


class FollowupHint(BaseModel):
    capability: str
    operation: str
    target_node_ids: list[GraphNodeId] = Field(default_factory=list)
    reason: str
    source_refs: list[str] = Field(default_factory=list)


class AgentResult(BaseModel):
    work_id: str
    outcome: Literal["succeeded", "failed", "blocked", "partial"]
    evidence_ids: list[EvidenceId] = Field(default_factory=list)
    changed_node_ids: list[GraphNodeId] = Field(default_factory=list)
    finding_ids: list[FindingId] = Field(default_factory=list)
    proposed_followups: list[FollowupHint] = Field(default_factory=list)
    retry_class: Literal["never", "transient", "input_changed", "human_required"] = "never"
    graph_delta_count: int = Field(default=0, ge=0)
    evidence_delta_count: int = Field(default=0, ge=0)
    compact_summary: str = ""
