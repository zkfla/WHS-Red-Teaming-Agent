"""Revisioned hypothesis writes, independent of observation step ordering."""

from datetime import datetime

from redteam.attack_graph.identity import hypothesis_node_id
from redteam.attack_graph.predicates import (
    MAX_PROVENANCE_ENTRIES,
    Provenance,
    build_properties,
    facts_of,
)
from redteam.core.contracts.graph import GraphNode, Hypothesis, HypothesisUpdate
from redteam.core.enums import GraphNodeType


class HypothesisConflict(ValueError):
    """Structured write rejection; never contains the candidate body."""


def hypothesis_purpose(hypothesis: Hypothesis) -> dict:
    """Meaning to which an independent verdict receipt must be bound."""
    return {"description": hypothesis.description, "premise": hypothesis.premise,
            "targets": sorted(hypothesis.target_node_ids),
            "principals": sorted(hypothesis.principal_refs)}


def candidate_facts_changed(previous: GraphNode | None, proposed: GraphNode) -> bool:
    before = facts_of(previous.properties) if previous else {}
    after = facts_of(proposed.properties)
    return proposed.type is GraphNodeType.HYPOTHESIS and (
        before.get("contract_version") == 2 or after.get("contract_version") == 2
    ) and before != after


def prepare_hypothesis_update(
    current: GraphNode | None, update: HypothesisUpdate, *, now: datetime, run_id: str,
) -> GraphNode:
    """Called only by central admission after reference/review validation."""
    update = HypothesisUpdate.model_validate(update.model_dump())
    candidate = update.hypothesis
    previous = Hypothesis.model_validate(facts_of(current.properties)) if current else None
    if (previous is None) != (update.expected_revision is None) or (
        previous is not None and previous.revision != update.expected_revision
    ):
        raise HypothesisConflict("candidate_revision_changed")
    if current is not None and current.type is not GraphNodeType.HYPOTHESIS:
        raise HypothesisConflict("candidate_identity_conflict")
    values = candidate.model_dump()
    values["revision"] = (previous.revision if previous else 0) + 1
    if previous is not None:
        values["metadata"] = {**previous.metadata, **candidate.metadata}
        for name in ("supporting_evidence_ids", "refuting_evidence_ids", "source_hypothesis_ids"):
            values[name] = sorted(set(getattr(previous, name)) | set(getattr(candidate, name)))
    if update.review_evidence_id:
        values["metadata"]["candidate_review_evidence_id"] = update.review_evidence_id
    saved = Hypothesis.model_validate(values)
    properties = build_properties(
        attrs={"status": saved.status}, facts=saved.model_dump(mode="json"), step=None,
        provenance=Provenance(source="planner:candidate", run_id=run_id),
    )
    if current is not None:
        # Retain unrelated graph history; replace candidate facts directly, never
        # merge by the worker's local step or concatenate semantic text fields.
        properties = {**current.properties, **properties,
                      "provenance": [*current.properties.get("provenance", []),
                                     *properties["provenance"]]}
        properties["first_seen_step"] = current.properties.get("first_seen_step", 0)
        properties["last_seen_step"] = current.properties.get("last_seen_step", 0)
        properties["provenance"] = properties["provenance"][-MAX_PROVENANCE_ENTRIES:]
    return GraphNode(
        id=current.id if current else hypothesis_node_id(saved.hypothesis_id),
        type=GraphNodeType.HYPOTHESIS, labels=current.labels if current else [],
        properties=properties, created_at=current.created_at if current else now, updated_at=now,
    )
