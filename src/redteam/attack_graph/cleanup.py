"""Tracking resources a run created, so they get cleaned up.

A test that creates accounts, cards or files and leaves them behind has
changed the target system and not said so. The obligation is real, so it
is recorded as state rather than left to whoever remembers: the item
lives on the node itself (``properties["cleanup"]``), which means it
survives a snapshot round trip and shows up in the integrity report.

``check_integrity`` treats an uncleaned resource as blocking — a report
must not go out while the run is still holding something.

Owned by Agent Team. Rationale: ``docs/agent/attack-graph.md``.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from redteam.attack_graph.predicates import (
    CLEANUP_CLEANED,
    CLEANUP_FAILED,
    CLEANUP_NOT_REQUIRED,
    CLEANUP_PENDING,
    cleanup_of,
)
from redteam.core.contracts.graph import GraphNode
from redteam.core.ids import GraphNodeId


class CleanupStatus(str, Enum):
    PENDING = CLEANUP_PENDING
    CLEANED = CLEANUP_CLEANED
    FAILED = CLEANUP_FAILED
    NOT_REQUIRED = CLEANUP_NOT_REQUIRED


class CleanupItem(BaseModel):
    """One resource the run is responsible for removing."""

    node_id: GraphNodeId
    resource_type: str
    status: CleanupStatus = CleanupStatus.PENDING
    created_at_step: int = 0
    created_by_action_id: str | None = None
    #: Evidence that the removal actually happened — a claim of cleanup
    #: with nothing behind it is the same problem as an ungrounded finding.
    cleanup_evidence_id: str | None = None
    note: str = ""

    @property
    def is_outstanding(self) -> bool:
        return self.status in (CleanupStatus.PENDING, CleanupStatus.FAILED)


class CleanupReport(BaseModel):
    items: list[CleanupItem] = Field(default_factory=list)

    @property
    def outstanding(self) -> list[CleanupItem]:
        return [item for item in self.items if item.is_outstanding]

    @property
    def is_clear(self) -> bool:
        return not self.outstanding

    def describe(self) -> str:
        if self.is_clear:
            return f"{len(self.items)} tracked, nothing outstanding"
        listed = ", ".join(f"{item.node_id}({item.resource_type})" for item in self.outstanding)
        return f"{len(self.outstanding)} outstanding: {listed}"


def item_for(node: GraphNode) -> CleanupItem | None:
    """Read the cleanup record off a node, if it carries one."""
    raw = cleanup_of(node.properties)
    if raw is None:
        return None
    return CleanupItem.model_validate({"node_id": node.id, **raw})


def collect(nodes: list[GraphNode]) -> CleanupReport:
    items = [item for item in (item_for(node) for node in nodes) if item is not None]
    return CleanupReport(items=sorted(items, key=lambda item: str(item.node_id)))
