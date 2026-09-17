from datetime import datetime, timezone

from redteam.core.contracts.target import HostTarget
from redteam.core.enums import RiskLevel, ToolHealthStatus
from redteam.orchestration.candidates.models import (
    CandidateDraft,
    ExecutableCandidate,
    PriorityScore,
)
from redteam.orchestration.difficulty import (
    apply_difficulty,
    estimate_difficulty,
    evaluate_branch_pruning,
)
from redteam.orchestration.work.models import WorkItem, WorkStatus
from tests.unit.test_orchestration_candidates import agent


def candidate() -> ExecutableCandidate:
    step = agent().playbook[0].bound_to(HostTarget(host="a.local"))
    draft = CandidateDraft(
        agent_id=agent().agent_id,
        plan_step=step,
        target_node_ids=["host:a.local"],
    )
    return ExecutableCandidate(
        candidate_key="cand_same",
        draft=draft,
        tool={
            "tool_id": "recon.ping",
            "server_id": "example",
            "tool_name": "ping",
            "risk_level": RiskLevel.LOW,
            "health_status": ToolHealthStatus.HEALTHY,
        },
        priority=PriorityScore(total=0),
    )


def work(status: WorkStatus) -> WorkItem:
    now = datetime.now(timezone.utc)
    return WorkItem(
        work_id=f"work_{status.value}",
        candidate_key="cand_same",
        agent_id=agent().agent_id,
        target_node_ids=["host:a.local"],
        priority=PriorityScore(total=0),
        status=status,
        created_at=now,
        updated_at=now,
    )


def test_failures_raise_difficulty_and_repetition_penalty() -> None:
    item = candidate()
    fresh = estimate_difficulty(item, [], context_load=0.1)
    repeated = estimate_difficulty(
        item,
        [work(WorkStatus.FAILED), work(WorkStatus.BLOCKED)],
        context_load=0.1,
    )

    assert repeated.total > fresh.total
    assert repeated.historical_success < fresh.historical_success
    assert repeated.repetition_penalty > 0


def test_estimate_is_written_into_explainable_priority_components() -> None:
    item = candidate()
    estimate = estimate_difficulty(item, [work(WorkStatus.SUCCEEDED)], context_load=0.5)
    reranked = apply_difficulty(item, estimate)

    assert reranked.draft.signals.estimated_difficulty == estimate.total
    assert "estimated_difficulty" in reranked.priority.components
    assert "work-ledger:difficulty" in reranked.draft.signals.source_refs


def test_repeated_failed_difficult_repeatable_branch_is_pruned() -> None:
    item = candidate()
    item.draft.plan_step.repeatable = True
    item.draft.signals.estimated_difficulty = 0.8
    item.draft.signals.objective_progress = 0.4

    decision = evaluate_branch_pruning(
        item,
        [work(WorkStatus.FAILED), work(WorkStatus.BLOCKED)],
        failure_threshold=2,
        difficulty_threshold=0.65,
        objective_override=0.8,
    )

    assert decision.pruned is True
    assert decision.related_failures == 2


def test_high_objective_branch_is_not_pruned() -> None:
    item = candidate()
    item.draft.plan_step.repeatable = True
    item.draft.signals.estimated_difficulty = 0.9
    item.draft.signals.objective_progress = 0.9

    decision = evaluate_branch_pruning(
        item,
        [work(WorkStatus.FAILED), work(WorkStatus.BLOCKED)],
        failure_threshold=2,
        difficulty_threshold=0.65,
        objective_override=0.8,
    )

    assert decision.pruned is False
