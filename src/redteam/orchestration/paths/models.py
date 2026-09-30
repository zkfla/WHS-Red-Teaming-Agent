"""Investigation-path structure models and structural validation."""

from __future__ import annotations

import networkx as nx
from pydantic import BaseModel, Field, field_validator


class PathError(Exception):
    """Base error for investigation-path storage failures."""


class PathValidationError(PathError):
    """Raised when a path plan or step update fails structural checks."""


class PathVersionConflict(PathError):
    """Raised on duplicate versions or stale optimistic-concurrency checks."""


#: Canonical path lifecycle states. Only ``active`` holds the per-run pointer.
#: ``draft`` is an unreviewed sketch; ``candidate`` awaits review; ``active``
#: is the version under investigation; ``rejected`` failed review;
#: ``superseded`` was replaced by a newer version. Snapshots stay immutable,
#: so replaced versions keep their stored status while the pointer moves on.
PATH_STATUSES = frozenset({"draft", "candidate", "active", "rejected", "superseded"})

#: Legacy alias accepted on input and normalized to ``active``.
PATH_STATUS_ALIASES = {"accepted": "active"}

#: Canonical step progress states. ``pending`` waits; ``running`` executes;
#: ``completed``/``failed``/``blocked``/``skipped`` are terminal progress marks.
#: Progress lives per version and merges over the immutable snapshot on read.
STEP_STATUSES = frozenset(
    {"pending", "running", "completed", "failed", "blocked", "skipped"}
)

#: Legacy alias accepted on input and normalized to ``running``.
STEP_STATUS_ALIASES = {"in_progress": "running"}

#: Provable completion-criterion kinds. Anything else normalizes to
#: ``"unknown"`` and can never confirm success (checked by completion
#: verification, not by the store).
ALLOWED_CRITERION_KINDS = frozenset(
    {
        "evidence",
        "observation",
        "hypothesis_confirmed",
        "hypothesis_refuted",
        "coverage",
        "poc",
    }
)

#: Marker for unprovable completion conditions.
UNKNOWN_CRITERION_KIND = "unknown"


def normalize_path_status(status: str) -> str:
    """Map the legacy ``accepted`` alias to ``active``; pass through else."""
    return PATH_STATUS_ALIASES.get(status, status)


def normalize_step_status(status: str) -> str:
    """Map the legacy ``in_progress`` alias to ``running``; pass through else."""
    return STEP_STATUS_ALIASES.get(status, status)


def is_active_status(status: str) -> bool:
    """Return True for ``active`` and the legacy ``accepted`` alias."""
    return normalize_path_status(status) == "active"


class CompletionCriterion(BaseModel):
    """Structured completion condition for one investigation step."""

    kind: str = "unknown"
    description: str = ""
    min_evidence: int = Field(default=1, ge=1)
    hypothesis_id: str | None = None
    principal_refs: list[str] = []

    @field_validator("kind")
    @classmethod
    def _normalize_kind(cls, value: str) -> str:
        if value not in ALLOWED_CRITERION_KINDS:
            return UNKNOWN_CRITERION_KIND
        return value

    def is_known(self) -> bool:
        """Return True only for a provable kind (never ``unknown``)."""
        return self.kind in ALLOWED_CRITERION_KINDS


def is_known_criterion(criterion: CompletionCriterion | str) -> bool:
    """Return True only for a structured criterion with a provable kind."""
    return isinstance(criterion, CompletionCriterion) and criterion.is_known()


def _single_criterion_key(criterion: CompletionCriterion | str) -> str:
    if isinstance(criterion, str):
        return "str:" + criterion
    refs = ",".join(sorted(criterion.principal_refs))
    return (
        "model:" + criterion.kind + "|" + criterion.description
        + "|" + str(criterion.min_evidence) + "|" + str(criterion.hypothesis_id)
        + "|" + refs
    )


def criteria_key(criterion: object) -> str:
    """Normalized comparison form for completion criteria."""
    if isinstance(criterion, list):
        parts = sorted(_single_criterion_key(item) for item in criterion)
        return "list:" + "|".join(parts)
    if isinstance(criterion, (CompletionCriterion, str)):
        return _single_criterion_key(criterion)
    raise PathValidationError(f"invalid completion criteria: {criterion!r}")


class PathStep(BaseModel):
    """One investigation step (a DAG node) inside a path version."""

    step_id: str
    objective: str
    agent_id: str | None = None
    target_node_ids: list[str] = []
    principal_refs: list[str] = []
    allowed_operations: list[str] = []
    depends_on: list[str] = []
    required_fact_refs: list[str] = []
    unknowns: list[str] = []
    completion_criteria: list[CompletionCriterion] = []
    required: bool = True
    status: str = "pending"
    outcome: str | None = None
    evidence_ids: list[str] = []

    @field_validator("completion_criteria", mode="before")
    @classmethod
    def _coerce_criteria(cls, value: object) -> object:
        if value is None:
            return []
        if isinstance(value, (str, dict, CompletionCriterion)):
            value = [value]
        if isinstance(value, (list, tuple)):
            coerced: list[object] = []
            for item in value:
                if isinstance(item, str):
                    coerced.append({"kind": "unknown", "description": item})
                elif isinstance(item, (dict, CompletionCriterion)):
                    coerced.append(item)
                else:
                    raise ValueError(f"invalid completion criterion: {item!r}")
            return coerced
        raise ValueError(f"invalid completion_criteria: {value!r}")


class PathPlan(BaseModel):
    """One versioned investigation path: immutable snapshot plus metadata."""

    path_id: str
    run_id: str
    mission_id: str | None = None
    version: int
    parent_version: int | None = None
    status: str
    reason: str
    goal: str
    steps: list[PathStep]
    hypothesis_ids: list[str] = []
    evidence_ids: list[str] = []
    changed_step_ids: list[str] = []
    review_records: dict = {}


def step_definition_equal(first: PathStep, second: PathStep) -> bool:
    """Return True when two steps share revision-defining fields.

    Compares objective, depends_on, completion_criteria (normalized),
    allowed_operations, target_node_ids, principal_refs, required_fact_refs,
    agent_id, required, and unknowns. Progress fields (status, outcome,
    evidence_ids) are excluded: identical definitions carry progress over.
    A changed target or principal resets the step so stale evidence can
    never confirm the new definition.
    """
    return (
        first.objective == second.objective
        and sorted(first.depends_on) == sorted(second.depends_on)
        and criteria_key(first.completion_criteria)
        == criteria_key(second.completion_criteria)
        and sorted(first.allowed_operations) == sorted(second.allowed_operations)
        and sorted(first.target_node_ids) == sorted(second.target_node_ids)
        and sorted(first.principal_refs) == sorted(second.principal_refs)
        and sorted(first.required_fact_refs) == sorted(second.required_fact_refs)
        and first.agent_id == second.agent_id
        and first.required == second.required
        and sorted(first.unknowns) == sorted(second.unknowns)
    )


def validate_path_structure(steps: list[PathStep]) -> None:
    """Check step DAG structure; raise PathValidationError on any defect."""
    counts: dict[str, int] = {}
    for step in steps:
        counts[step.step_id] = counts.get(step.step_id, 0) + 1
    duplicated = sorted(sid for sid, count in counts.items() if count > 1)
    if duplicated:
        raise PathValidationError(f"duplicate step_id: {duplicated}")
    known = set(counts)
    for step in steps:
        for dependency in step.depends_on:
            if dependency == step.step_id:
                raise PathValidationError(f"self dependency: {step.step_id}")
            if dependency not in known:
                raise PathValidationError(
                    f"unknown depends_on: {dependency} (step {step.step_id})"
                )
    graph = nx.DiGraph()
    graph.add_nodes_from(known)
    for step in steps:
        for dependency in step.depends_on:
            graph.add_edge(dependency, step.step_id)
    if not nx.is_directed_acyclic_graph(graph):
        try:
            cycle = nx.find_cycle(graph, orientation="original")
            cycle_nodes = [edge[0] for edge in cycle]
        except nx.NetworkXNoCycle:
            cycle_nodes = []
        raise PathValidationError(f"dependency cycle detected: {cycle_nodes}")
