"""Shared Attack Graph domain model: nodes, edges, hypotheses, findings.

Owned by PM (schema) — Agent Team owns the store/service in
``redteam.attack_graph``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from redteam.core.enums import GraphEdgeType, GraphNodeType
from redteam.core.ids import GraphEdgeId, GraphNodeId


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
    hypothesis_id: str
    description: str
    confidence: float = 0.0
    status: str = "open"
    metadata: dict[str, Any] = Field(default_factory=dict)


class Finding(BaseModel):
    finding_id: str
    title: str
    description: str = ""
    severity: str = "info"
    verified: bool = False
    evidence_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
