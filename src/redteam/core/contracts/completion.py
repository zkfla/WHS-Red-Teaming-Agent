"""Versioned, code-owned task completion conditions (not execution permission)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, Field, field_validator

ALLOWED_CRITERION_KINDS = frozenset({
    "evidence", "observation", "hypothesis_confirmed", "hypothesis_refuted",
    "coverage", "poc", "semantic",
})
UNKNOWN_CRITERION_KIND = "unknown"


class CompletionCriterion(BaseModel):
    """A fixed condition. Unknown legacy kinds remain explicitly unprovable."""

    kind: str = UNKNOWN_CRITERION_KIND
    description: str = ""
    min_evidence: int = Field(default=1, ge=1)
    hypothesis_id: str | None = None
    principal_refs: list[str] = Field(default_factory=list)

    @field_validator("kind")
    @classmethod
    def _normalize_kind(cls, value: str) -> str:
        return value if value in ALLOWED_CRITERION_KINDS else UNKNOWN_CRITERION_KIND

    def is_known(self) -> bool:
        return self.kind in ALLOWED_CRITERION_KINDS


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def criterion_id(criterion: CompletionCriterion) -> str:
    """Stable across serialization and order-only principal changes."""
    payload = criterion.model_dump(mode="json")
    payload["principal_refs"] = sorted(set(criterion.principal_refs))
    return "criterion_" + _digest(payload)


def completion_contract_hash(task: Any) -> str:
    """Purpose contract only; runtime status, ownership and revision are excluded."""
    purpose = {
        "version": task.completion_contract_version,
        "objective": task.objective.strip(),
        "criteria": sorted(criterion_id(item) for item in task.completion_criteria),
        "targets": sorted(map(str, task.related_attack_graph_node_ids)),
        "principals": sorted(set(task.principal_refs)),
        "hypotheses": sorted(map(str, task.related_hypothesis_ids)),
    }
    if getattr(task, "investigation_contract_version", 1) == 2:
        purpose["investigation_version"] = 2
        purpose["hypothesis_revisions"] = dict(task.hypothesis_revisions)
        if task.probe_limits is not None:
            purpose["probe_limits"] = task.probe_limits.model_dump(mode="json")
    return _digest(purpose)


def completion_conditions(task: Any) -> list[dict[str, Any]]:
    return [
        {"criterion_id": criterion_id(item), **item.model_dump(mode="json")}
        for item in task.completion_criteria
    ]
