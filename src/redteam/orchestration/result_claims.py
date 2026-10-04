"""Actual result claims shared by task, goal and terminal reviews."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from redteam.orchestration.completion import validate_result_claims
from redteam.orchestration.coordination import content_hash
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
) -> list[str]:
    """Reuse actual origin, account, verdict and finding proofs per claimed result."""
    if not claims:
        return ["completion review describes no supported result"]
    available = set(evidence_ids)
    scope = set(target_node_ids or [])
    for value in claims:
        claim = ResultClaim.model_validate(value)
        if set(claim.evidence_ids) - available:
            return ["result claim cites unrelated evidence"]
        if scope and set(claim.target_node_ids) - scope:
            return ["result claim concerns a different task target"]
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
        )
        if errors:
            return errors
    return []
