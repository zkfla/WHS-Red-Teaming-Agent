"""Deciding which predicates no longer hold.

``predicates.apply_retraction`` can withdraw a predicate, but something
has to notice that it should be withdrawn. That is this module: rules say
"``reachable`` on a Host is only good for N steps", and ``plan_retractions``
turns the current graph into the list of withdrawals that are due.

Why it matters: predicates only ever accumulate otherwise, so the planner
keeps proposing actions whose preconditions stopped being true and burns
the budget failing.

Nothing here writes — the caller applies the plan, which keeps the rules
testable without a store and makes the decision visible before it lands.

Owned by Agent Team. Rationale: ``docs/agent/attack-graph.md``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from redteam.attack_graph.predicates import attrs_of, is_stale, last_seen_step
from redteam.core.contracts.graph import GraphNode
from redteam.core.enums import GraphNodeType
from redteam.core.ids import GraphNodeId


class RetractionRule(BaseModel):
    """"This predicate is only trustworthy for so long"."""

    attr: str
    max_age_steps: int = Field(ge=1)
    #: Limit the rule to one node type; ``None`` applies it everywhere.
    node_type: GraphNodeType | None = None
    reason: str = ""

    def applies_to(self, node: GraphNode) -> bool:
        if self.node_type is not None and node.type is not self.node_type:
            return False
        return self.attr in attrs_of(node.properties)

    def describe(self, node: GraphNode, current_step: int) -> str:
        age = current_step - last_seen_step(node.properties)
        base = self.reason or f"{self.attr!r} was last observed {age} steps ago"
        return f"{base} (limit {self.max_age_steps})"


class PlannedRetraction(BaseModel):
    node_id: GraphNodeId
    attr: str
    reason: str


#: Conservative defaults: only predicates that describe a live property of
#: the target, and only after enough turns that a re-probe is cheap
#: relative to acting on a stale belief.
DEFAULT_RETRACTION_RULES: tuple[RetractionRule, ...] = (
    RetractionRule(
        attr="reachable",
        max_age_steps=10,
        node_type=GraphNodeType.HOST,
        reason="host reachability has not been re-confirmed",
    ),
    RetractionRule(
        attr="reachable",
        max_age_steps=10,
        node_type=GraphNodeType.ENDPOINT,
        reason="endpoint reachability has not been re-confirmed",
    ),
    RetractionRule(
        attr="valid",
        max_age_steps=5,
        node_type=GraphNodeType.CREDENTIAL,
        reason="credential validity has not been re-confirmed",
    ),
)


def plan_retractions(
    nodes: list[GraphNode],
    *,
    current_step: int,
    rules: tuple[RetractionRule, ...] | list[RetractionRule] | None = None,
) -> list[PlannedRetraction]:
    """Which predicates are due to be withdrawn, and why."""
    active = DEFAULT_RETRACTION_RULES if rules is None else tuple(rules)
    planned: list[PlannedRetraction] = []
    for node in sorted(nodes, key=lambda item: str(item.id)):
        for rule in active:
            if not rule.applies_to(node):
                continue
            if not is_stale(node.properties, current_step, max_age=rule.max_age_steps):
                continue
            planned.append(
                PlannedRetraction(
                    node_id=node.id,
                    attr=rule.attr,
                    reason=rule.describe(node, current_step),
                )
            )
    return planned
