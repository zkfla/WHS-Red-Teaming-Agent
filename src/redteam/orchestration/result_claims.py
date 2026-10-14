"""Actual result claims shared by task, goal and terminal reviews."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from redteam.attack_graph.identity import hypothesis_node_id
from redteam.core.ids import GraphEdgeId, GraphNodeId
from redteam.orchestration.analysis import claim_target_is_grounded
from redteam.orchestration.completion import preserved_target_is_absent, validate_result_claims
from redteam.orchestration.coordination import StateConflict, content_hash
from redteam.orchestration.paths import CompletionCriterion


def mission_result_purpose(mission: Any) -> str:
    """Bind stored meaning to current goal conditions, independent of revisions."""
    return content_hash(
        {
            "objective": mission.objective,
            "success_criteria": list(mission.success_criteria),
            "operator_constraints": mission.operator_constraints,
        }
    )


class ResultClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1)
    kind: Literal[
        "semantic", "observation", "hypothesis_confirmed", "hypothesis_refuted", "poc"
    ] = "semantic"
    evidence_ids: list[str] = Field(min_length=1)
    target_node_ids: list[str] = Field(default_factory=list)
    principal_refs: list[str] = Field(default_factory=list)
    hypothesis_id: str | None = None


class ResultReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accepted: bool
    reason: str
    claims: list[ResultClaim]


class CriterionReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    criterion_id: str
    met: bool
    evidence_ids: list[str] = Field(min_length=1)
    reason: str


class TaskContractReview(ResultReview):
    """v2 requires an explicit AND judgment over every stored condition."""

    criterion_results: list[CriterionReview] = Field(min_length=1)


async def capture_result_graph(service, claims=(), *, conditions=()):
    """Only graph subjects consumed by fixed criteria or explicit result claims."""
    identities = {str(hypothesis_node_id(item.hypothesis_id)) for item in conditions
                  if item.hypothesis_id}
    for raw in claims:
        claim = ResultClaim.model_validate(raw)
        identities.update(claim.target_node_ids)
        if claim.hypothesis_id:
            identities.add(str(hypothesis_node_id(claim.hypothesis_id)))
    return {identity: content_hash(await service.get_node(GraphNodeId(identity)))
            for identity in identities}


def check_result_graph(tx, proof):
    if any(content_hash(tx.graph.get_local_node(GraphNodeId(identity))) != expected
           for identity, expected in proof.items()):
        raise StateConflict("completion_graph_proof_changed_reassessment_required")


def check_result_edge_proof(tx, proof):
    """Final CAS over task-grounding relation edges proven before review."""
    for edge_id, expected in proof.items():
        edge = tx.graph.get_local_edge(GraphEdgeId(edge_id))
        if edge is None or content_hash(edge) != expected:
            raise StateConflict("completion_grounding_edge_changed_reassessment_required")


RESULT_REVIEW_INSTRUCTION = (
    " Describe actual results as claims tied to their exact source evidence. "
    "Use semantic for general investigation or synthesis, poc for a vulnerability "
    "success claim, and hypothesis_confirmed/hypothesis_refuted for those conclusions. "
    "Include the actual target, account and hypothesis references required by the "
    "original purpose. Accepting a result requires at least one supported claim. "
    "Provisional plan steps are methods; never require all of them to finish."
)


async def check_result_review(
    *,
    claims: list[Any],
    evidence_ids: list[str],
    evidence_store: Any,
    graph_service: Any,
    path_store: Any,
    run_id: str,
    mission_id: str | None,
    target_node_ids: list[str] | None = None,
    claims_location: tuple[str | int, ...] = ("claims",),
    task: Any | None = None,
    task_graph_store: Any | None = None,
    work_store: Any | None = None,
 ) -> list[str]:
    """Reuse actual origin, account, verdict and finding proofs per claimed result."""
    if not claims:
        return ["completion review describes no supported result"]
    available = set(evidence_ids)
    scope = set(target_node_ids or [])
    for claim_index, value in enumerate(claims):
        claim = ResultClaim.model_validate(value)
        if set(claim.evidence_ids) - available:
            return ["result claim cites unrelated evidence"]
        if scope and set(claim.target_node_ids) - scope:
            return ["result claim concerns a different task target"]
        for identity in claim.evidence_ids:
            record = await evidence_store.get(identity)
            if record is not None and preserved_target_is_absent(record.metadata):
                for target in claim.target_node_ids:
                    if not await claim_target_is_grounded(
                        graph_service, record, target, evidence_store=evidence_store,
                    ):
                        return ["target-free evidence does not ground the claimed target"]
        errors = await validate_result_claims(
            criteria=[
                CompletionCriterion(
                    kind=claim.kind,
                    description=claim.description,
                    hypothesis_id=claim.hypothesis_id,
                    principal_refs=claim.principal_refs,
                )
            ],
            evidence_ids=claim.evidence_ids,
            target_node_ids=claim.target_node_ids or list(target_node_ids or []),
            principal_refs=claim.principal_refs,
            evidence_store=evidence_store,
            graph_service=graph_service,
            run_id=run_id,
            path_store=path_store,
            mission_id=mission_id,
            evidence_location=(*claims_location, claim_index, "evidence_ids"),
            task=task,
            task_graph_store=task_graph_store,
            work_store=work_store,
        )
        if errors:
            return errors
    return []
