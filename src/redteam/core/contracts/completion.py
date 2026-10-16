"""Versioned, code-owned task completion conditions (not execution permission)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field, field_validator

ALLOWED_CRITERION_KINDS = frozenset({
    "evidence", "observation", "hypothesis_confirmed", "hypothesis_refuted",
    "coverage", "poc", "semantic",
})
UNKNOWN_CRITERION_KIND = "unknown"
MAX_COMPLETION_CONDITION_CONFLICTS = 8


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


@dataclass(frozen=True)
class CompletionConditionConflict:
    """One bounded, structured same-candidate verdict contradiction."""

    hypothesis_id: str
    criterion_ids: tuple[str, str]


def completion_condition_conflicts(
    criteria: Sequence[CompletionCriterion],
) -> tuple[CompletionConditionConflict, ...]:
    """Find explicit confirmation/refutation pairs for one candidate.

    Only structured kinds and matching hypothesis IDs participate. Descriptions,
    observations, established facts, and verdicts for separate candidates are
    intentionally left alone. Results are sorted and capped for safe diagnostics.
    """
    by_hypothesis: dict[str, dict[str, str | None]] = {}
    for criterion in criteria:
        if criterion.kind not in {"hypothesis_confirmed", "hypothesis_refuted"}:
            continue
        if not criterion.hypothesis_id:
            continue
        verdicts = by_hypothesis.setdefault(criterion.hypothesis_id, {
            "hypothesis_confirmed": None, "hypothesis_refuted": None,
        })
        identity = criterion_id(criterion)
        current = verdicts[criterion.kind]
        if current is None or identity < current:
            verdicts[criterion.kind] = identity

    conflicts: list[CompletionConditionConflict] = []
    for hypothesis_id, verdicts in sorted(by_hypothesis.items()):
        confirmed, refuted = verdicts["hypothesis_confirmed"], verdicts["hypothesis_refuted"]
        if not confirmed or not refuted:
            continue
        conflicts.append(CompletionConditionConflict(
            hypothesis_id=_diagnostic_reference(hypothesis_id),
            criterion_ids=(confirmed, refuted),
        ))
        if len(conflicts) == MAX_COMPLETION_CONDITION_CONFLICTS:
            break
    return tuple(conflicts)


def _diagnostic_reference(value: str) -> str:
    """Keep references bounded and identifier-shaped before exposing diagnostics."""
    if len(value) > 100 or not value or any(
        not (char.isascii() and (char.isalnum() or char in "_-")) for char in value
    ):
        return "invalid_reference"
    return value


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def criterion_id(criterion: CompletionCriterion) -> str:
    """Stable across serialization and order-only principal changes."""
    payload = criterion.model_dump(mode="json")
    payload["principal_refs"] = sorted(set(criterion.principal_refs))
    return "criterion_" + _digest(payload)


def _completion_purpose(task: Any, *, include_probe_limits: bool) -> dict[str, Any]:
    """Build the code-owned semantic purpose used by stable task identities."""
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
        if include_probe_limits and task.probe_limits is not None:
            purpose["probe_limits"] = task.probe_limits.model_dump(mode="json")
    return purpose


def completion_contract_hash(task: Any) -> str:
    """Purpose contract only; runtime status, ownership and revision are excluded."""
    purpose = _completion_purpose(task, include_probe_limits=True)
    return _digest(purpose)


def probe_failure_circuit_key(task: Any) -> str:
    """Return a purpose key that cannot be reset by changing only probe limits.

    Probe limits remain part of the completion contract and resume binding, but
    changing only those limits must not turn a cloned Task into a fresh provider
    circuit. All semantic purpose fields still distinguish genuinely different
    Tasks.
    """
    return _digest(_completion_purpose(task, include_probe_limits=False))


def contradictory_candidate_verdict(criteria: Sequence[CompletionCriterion]) -> str | None:
    """Compatibility alias for the first same-candidate confirmed/refuted contradiction.

    The rule and the bounded, sanitized reference come from
    ``completion_condition_conflicts``, so callers keep the structured guard.
    """
    conflicts = completion_condition_conflicts(criteria)
    return conflicts[0].hypothesis_id if conflicts else None


def completion_conditions(task: Any) -> list[dict[str, Any]]:
    return [
        {"criterion_id": criterion_id(item), **item.model_dump(mode="json")}
        for item in task.completion_criteria
    ]
