"""Preconditions over the Attack Graph: ``NodePattern`` + ``resolve``.

Pure functions over materialized node/edge lists — no store, no I/O.
Docs: ``docs/agent/attack-graph.md``.
"""

from __future__ import annotations

import itertools
from typing import Any

from pydantic import BaseModel, Field, model_validator

from redteam.attack_graph.predicates import attrs_of, is_stale
from redteam.core.contracts.graph import GraphEdge, GraphNode
from redteam.core.enums import GraphEdgeType, GraphNodeType
from redteam.core.ids import GraphNodeId

#: Ceiling on backtracking assignments per ``resolve`` call. The frontier
#: is recomputed every turn, so an unbounded search is a hang. Exhausting
#: it reports "could not decide", never "no match".
MAX_SEARCH_ASSIGNMENTS = 20_000


class SearchBudgetExceeded(Exception):
    """Raised internally when the backtracking budget runs out."""


class PatternDefinitionError(ValueError):
    """An authoring mistake that would resolve to a wrong answer rather
    than no answer, so it fails loudly."""


class NodePattern(BaseModel):
    """One precondition: "N distinct nodes of this type/shape must exist"."""

    type: GraphNodeType
    attrs: dict[str, bool | str | int] = Field(default_factory=dict)
    count: int = Field(default=1, ge=1)

    #: Name this pattern's matches are referred to by, in params and logs.
    bind: str = ""
    #: An unsatisfied optional pattern is skipped instead of failing.
    optional: bool = False

    #: Key in ``params`` naming the node(s) the caller chose.
    from_param: str = ""
    #: Other binds whose nodes this pattern's nodes must not overlap.
    distinct_from: list[str] = Field(default_factory=list)

    #: Bind on the *source* side of the required edge (``related_to -> self``).
    related_to: str = ""
    edge_type: GraphEdgeType | None = None
    #: Require the edge to be **absent** — for authorization-boundary tests.
    edge_absent: bool = False

    #: Re-observe if the newest match is older than this many steps (0 = off).
    max_age_steps: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _edge_fields_are_consistent(self) -> NodePattern:
        if self.edge_type is not None and not self.related_to:
            raise ValueError(f"edge_type={self.edge_type} set but related_to is empty")
        if self.related_to and self.edge_type is None:
            raise ValueError(f"related_to={self.related_to!r} set but edge_type is missing")
        if self.edge_absent and self.edge_type is None:
            raise ValueError("edge_absent requires edge_type")
        if self.bind and self.bind in self.distinct_from:
            raise ValueError(
                f"{self.bind!r} cannot be distinct from itself — drop it from distinct_from"
            )
        return self

    @property
    def name(self) -> str:
        return self.bind or self.type.value

    def describe(self) -> str:
        shape = f"{self.type.value}{self.attrs or ''}"
        return f"{shape} x{self.count}" if self.count > 1 else shape

    def matches(self, node: GraphNode) -> bool:
        if node.type != self.type:
            return False
        node_attrs = attrs_of(node.properties)
        return all(
            _same_value(node_attrs.get(key), value) for key, value in self.attrs.items()
        )


def _same_value(observed: object, wanted: object) -> bool:
    """Strict equality — ``True`` and ``1`` are not the same predicate."""
    if isinstance(wanted, bool) != isinstance(observed, bool):
        return False
    return observed == wanted


class ResolveResult(BaseModel):
    """Either a binding of pattern names to node ids, or why there isn't one."""

    ok: bool
    bindings: dict[str, list[GraphNodeId]] = Field(default_factory=dict)
    reason: str = ""

    def __bool__(self) -> bool:  # pragma: no cover - convenience only
        return self.ok

    def one(self, bind: str) -> GraphNodeId:
        """The single node bound to ``bind`` (raises if it isn't exactly one)."""
        ids = self.bindings.get(bind, [])
        if len(ids) != 1:
            raise KeyError(f"bind {bind!r} resolved to {len(ids)} nodes, expected exactly 1")
        return ids[0]


def validate_patterns(patterns: list[NodePattern]) -> None:
    """Reject precondition lists that silently resolve to the wrong thing."""
    seen: set[str] = set()
    for pattern in patterns:
        name = pattern.name
        if name in seen:
            raise PatternDefinitionError(
                f"two patterns both resolve to the name {name!r}; give each an explicit "
                f"`bind` or they overwrite one another"
            )
        seen.add(name)
        if name in pattern.distinct_from:
            raise PatternDefinitionError(f"{name!r} cannot be distinct from itself")

    for pattern in patterns:
        for other in pattern.distinct_from:
            if other not in seen:
                raise PatternDefinitionError(
                    f"{pattern.name!r} must be distinct from {other!r}, which is not a "
                    f"declared bind"
                )
        if pattern.related_to and pattern.related_to not in seen:
            raise PatternDefinitionError(
                f"{pattern.name!r} relates to {pattern.related_to!r}, which is not a "
                f"declared bind"
            )


def _wanted_ids(param_value: Any) -> set[str]:
    if isinstance(param_value, str):
        return {param_value}
    if isinstance(param_value, (list, tuple, set)):
        return {str(item) for item in param_value}
    return {str(param_value)}


def _candidates(
    pattern: NodePattern,
    nodes: list[GraphNode],
    *,
    current_step: int,
    params: dict[str, Any] | None,
    for_frontier: bool,
) -> tuple[list[GraphNode], str]:
    hits = [node for node in nodes if pattern.matches(node)]
    why = ""

    if pattern.from_param and not for_frontier:
        # Availability checks run before the caller has chosen params, so
        # `from_param` is only enforced when actually binding.
        chosen = (params or {}).get(pattern.from_param)
        if chosen is None:
            return [], (
                f"params has no {pattern.from_param!r}, so bind {pattern.name!r} "
                f"cannot be pinned to the node that was chosen"
            )
        wanted = _wanted_ids(chosen)
        hits = [node for node in hits if str(node.id) in wanted]
        if not hits:
            why = (
                f"params[{pattern.from_param!r}]={chosen!r} does not match "
                f"pattern {pattern.describe()}"
            )

    if pattern.max_age_steps > 0:
        fresh = [
            node
            for node in hits
            if not is_stale(node.properties, current_step, max_age=pattern.max_age_steps)
        ]
        if hits and not fresh:
            why = f"last observation is older than {pattern.max_age_steps} steps — re-observe first"
        hits = fresh

    return hits, why


def _edge_exists(
    edges: list[GraphEdge],
    source_ids: list[GraphNodeId],
    target_ids: list[GraphNodeId],
    edge_type: GraphEdgeType,
) -> bool:
    sources, targets = set(source_ids), set(target_ids)
    return any(
        edge.type == edge_type and edge.source_id in sources and edge.target_id in targets
        for edge in edges
    )


def resolve(
    patterns: list[NodePattern],
    *,
    nodes: list[GraphNode],
    edges: list[GraphEdge] | None = None,
    current_step: int = 0,
    params: dict[str, Any] | None = None,
    for_frontier: bool = False,
) -> ResolveResult:
    """Bind every pattern to concrete nodes, or explain why it can't.

    ``for_frontier=True`` answers availability (ignoring ``from_param``);
    the default answers commitment.
    """
    edges = edges or []
    named = [(pattern.name, pattern) for pattern in patterns]
    validate_patterns(patterns)

    candidates: dict[str, list[GraphNode]] = {}
    active: list[tuple[str, NodePattern]] = []
    for name, pattern in named:
        hits, why = _candidates(
            pattern,
            nodes,
            current_step=current_step,
            params=params,
            for_frontier=for_frontier,
        )
        if len(hits) < pattern.count:
            if pattern.optional:
                continue
            detail = (
                f"precondition not met: {pattern.describe()} needs "
                f"{pattern.count}, found {len(hits)}"
            )
            if why:
                detail += f" — {why}"
            return ResolveResult(ok=False, reason=detail)
        candidates[name] = hits
        active.append((name, pattern))

    # `distinct_from` is symmetric: declaring it once constrains both sides.
    exclusive: dict[str, set[str]] = {name: set() for name, _ in active}
    for name, pattern in active:
        for other in pattern.distinct_from:
            if other in exclusive:
                exclusive[name].add(other)
                exclusive[other].add(name)

    # If the referenced bind was skipped (unsatisfied `optional`), the
    # constraint would silently disappear — and with `edge_absent` that is
    # an authorization boundary check quietly passing.
    active_names = {name for name, _ in active}
    surviving: list[tuple[str, NodePattern]] = []
    for name, pattern in active:
        if pattern.edge_type is not None and pattern.related_to not in active_names:
            if not pattern.optional:
                return ResolveResult(
                    ok=False,
                    reason=(
                        f"precondition not met: {name!r} requires an edge to "
                        f"{pattern.related_to!r}, which has no candidates"
                    ),
                )
            candidates.pop(name, None)
            continue
        surviving.append((name, pattern))
    active = surviving
    active_names = {name for name, _ in active}
    for name in list(exclusive):
        if name not in active_names:
            exclusive.pop(name)
        else:
            exclusive[name] &= active_names

    edge_constraints = [
        (name, pattern.related_to, pattern.edge_type, pattern.edge_absent)
        for name, pattern in active
        if pattern.edge_type is not None
    ]

    # Counting rules this out before backtracking enumerates every
    # combination to prove the same thing.
    shortfall = _exclusion_shortfall(active, candidates, exclusive)
    if shortfall is not None:
        return ResolveResult(ok=False, reason=shortfall)

    # Fewest candidates first: fail fast, search less.
    order = sorted(active, key=lambda item: (len(candidates[item[0]]), item[1].count))
    placed: dict[str, list[GraphNodeId]] = {}
    budget = [MAX_SEARCH_ASSIGNMENTS]

    def edge_ok(
        target_ids: list[GraphNodeId],
        source_ids: list[GraphNodeId],
        edge_type: GraphEdgeType,
        absent: bool,
    ) -> bool:
        found = _edge_exists(edges, source_ids, target_ids, edge_type)
        return (not found) if absent else found

    def consistent(name: str, chosen: tuple[GraphNodeId, ...]) -> bool:
        taken = {
            node_id for other in exclusive[name] for node_id in placed.get(other, [])
        }
        if taken & set(chosen):
            return False

        trial = {**placed, name: list(chosen)}
        for owner, other, edge_type, absent in edge_constraints:
            if name not in (owner, other):
                continue
            if owner not in trial or other not in trial:
                continue  # the other side isn't placed yet (or was optional)
            assert edge_type is not None
            if not edge_ok(trial[owner], trial[other], edge_type, absent):
                return False
        return True

    def search(index: int) -> bool:
        if index == len(order):
            return True
        name, pattern = order[index]
        pool = [node.id for node in candidates[name]]
        for combo in itertools.combinations(pool, pattern.count):
            budget[0] -= 1
            if budget[0] <= 0:
                raise SearchBudgetExceeded
            if not consistent(name, combo):
                continue
            placed[name] = list(combo)
            if search(index + 1):
                return True
            placed.pop(name)
        return False

    counts = {name: len(hits) for name, hits in candidates.items()}
    try:
        found = search(0)
    except SearchBudgetExceeded:
        return ResolveResult(
            ok=False,
            reason=(
                f"precondition undecided: gave up after {MAX_SEARCH_ASSIGNMENTS} assignments "
                f"(candidates {counts}). Narrow the patterns — this is not the same as "
                f"'no match'."
            ),
        )

    if not found:
        return ResolveResult(
            ok=False,
            reason=(
                "precondition not met: every pattern has candidates, but no assignment "
                f"satisfies the distinct/edge constraints at once (candidates {counts})"
            ),
        )

    return ResolveResult(ok=True, bindings=placed)


def _exclusion_shortfall(
    active: list[tuple[str, NodePattern]],
    candidates: dict[str, list[GraphNode]],
    exclusive: dict[str, set[str]],
) -> str | None:
    """Can a group of non-overlapping binds fit at all? A mutually-exclusive
    group needs ``sum(count)`` distinct candidates between its members."""
    patterns = dict(active)
    seen: set[str] = set()

    for name in patterns:
        if name in seen:
            continue
        group = {name}
        queue = [name]
        while queue:
            current = queue.pop()
            for other in exclusive.get(current, set()):
                if other not in group and other in patterns:
                    group.add(other)
                    queue.append(other)
        seen |= group
        if len(group) < 2:
            continue

        needed = sum(patterns[member].count for member in group)
        pool = {node.id for member in group for node in candidates[member]}
        if needed > len(pool):
            listed = ", ".join(sorted(group))
            return (
                f"precondition not met: binds [{listed}] must not overlap and need "
                f"{needed} distinct nodes, but only {len(pool)} are available"
            )
    return None
