"""Explainable runtime difficulty telemetry for candidate reranking."""

from __future__ import annotations

from pydantic import BaseModel, Field

from redteam.core.enums import ToolHealthStatus
from redteam.orchestration.candidates.models import ExecutableCandidate
from redteam.orchestration.candidates.scoring import score_candidate
from redteam.orchestration.work.models import WorkItem, WorkStatus


class DifficultyEstimate(BaseModel):
    total: float = Field(ge=0.0, le=1.0)
    components: dict[str, float]
    historical_success: float = Field(ge=0.0, le=1.0)
    repetition_penalty: float = Field(ge=0.0, le=1.0)


class BranchPruneDecision(BaseModel):
    pruned: bool
    reason: str | None = None
    related_failures: int = Field(default=0, ge=0)


def evaluate_branch_pruning(
    candidate: ExecutableCandidate,
    history: list[WorkItem],
    *,
    failure_threshold: int,
    difficulty_threshold: float,
    objective_override: float,
) -> BranchPruneDecision:
    """Prune only a repeatedly failing, expensive repeatable branch.

    Non-repeatable candidates are handled by ledger identity.  High-objective
    candidates stay eligible even when difficult so the heuristic cannot cut
    the only promising closer merely because it is expensive.
    """

    if not candidate.plan_step.repeatable:
        return BranchPruneDecision(pruned=False)
    if candidate.draft.signals.objective_progress >= objective_override:
        return BranchPruneDecision(pruned=False)
    if candidate.draft.signals.estimated_difficulty < difficulty_threshold:
        return BranchPruneDecision(pruned=False)

    failures = sum(
        item.candidate_key == candidate.candidate_key
        and item.status in {WorkStatus.FAILED, WorkStatus.BLOCKED, WorkStatus.STALE}
        for item in history
    )
    if failures < failure_threshold:
        return BranchPruneDecision(pruned=False, related_failures=failures)
    return BranchPruneDecision(
        pruned=True,
        reason="repeated_failed_high_difficulty_branch",
        related_failures=failures,
    )


def estimate_difficulty(
    candidate: ExecutableCandidate,
    history: list[WorkItem],
    *,
    context_load: float,
) -> DifficultyEstimate:
    """Estimate from ledger telemetry only; no LLM-authored score is trusted."""

    target_ids = set(candidate.draft.target_node_ids)
    related = [
        item
        for item in history
        if item.agent_id == candidate.draft.agent_id
        and not target_ids.isdisjoint(item.target_node_ids)
    ]
    succeeded = sum(item.status is WorkStatus.SUCCEEDED for item in related)
    failed = sum(
        item.status in {WorkStatus.FAILED, WorkStatus.BLOCKED, WorkStatus.STALE}
        for item in related
    )
    decided = succeeded + failed
    historical_success = (succeeded + 1) / (decided + 2)
    failure_rate = 0.0 if decided == 0 else (failed + 1) / (decided + 2)
    same_candidate = sum(item.candidate_key == candidate.candidate_key for item in history)
    repetition = min(same_candidate / 3, 1.0)
    horizon = min(len(candidate.draft.prerequisite_ids) / 5, 1.0)
    tool_unreliability = {
        ToolHealthStatus.HEALTHY: 0.0,
        ToolHealthStatus.UNKNOWN: 0.5,
        ToolHealthStatus.UNHEALTHY: 1.0,
    }[candidate.tool.health_status]
    components = {
        "remaining_horizon": 0.2 * horizon,
        "recent_failure_rate": 0.4 * failure_rate,
        "projected_context_load": 0.2 * min(max(context_load, 0.0), 1.0),
        "tool_unreliability": 0.2 * tool_unreliability,
    }
    return DifficultyEstimate(
        total=round(sum(components.values()), 6),
        components={name: round(value, 6) for name, value in components.items()},
        historical_success=round(historical_success, 6),
        repetition_penalty=round(repetition, 6),
    )


def apply_difficulty(
    candidate: ExecutableCandidate,
    estimate: DifficultyEstimate,
) -> ExecutableCandidate:
    draft = candidate.draft.model_copy(deep=True)
    draft.signals.estimated_difficulty = estimate.total
    draft.signals.historical_success = estimate.historical_success
    draft.signals.repetition_penalty = estimate.repetition_penalty
    if "work-ledger:difficulty" not in draft.signals.source_refs:
        draft.signals.source_refs.append("work-ledger:difficulty")
    return candidate.model_copy(
        update={
            "draft": draft,
            "priority": score_candidate(
                draft,
                risk_level=candidate.tool.risk_level,
                health_status=candidate.tool.health_status,
            ),
        }
    )
