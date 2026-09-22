"""Explicit run convergence over scheduler and canonical graph state."""

from __future__ import annotations

from pydantic import BaseModel, Field

from redteam.attack_graph.service import AttackGraphService
from redteam.core.enums import AgentPhase
from redteam.core.ids import RunId
from redteam.orchestration.state import TerminationReason
from redteam.orchestration.work.models import ACTIVE_STATUSES
from redteam.orchestration.work.store import WorkStore


class ConvergenceDecision(BaseModel):
    terminal: bool
    phase: AgentPhase = AgentPhase.OBSERVE
    reason: str | None = None
    blockers: list[str] = Field(default_factory=list)
    active_work_count: int = 0
    unresolved_hypothesis_count: int = 0
    outstanding_cleanup_count: int = 0


async def evaluate_convergence(
    *,
    has_runnable_agent: bool,
    graph: AttackGraphService,
    work_store: WorkStore,
    run_id: RunId,
    no_agent_reason: str = TerminationReason.NO_ACTIONABLE_PLAN,
) -> ConvergenceDecision:
    """Distinguish real quiescence from orphaned or unreportable state."""

    if has_runnable_agent:
        return ConvergenceDecision(terminal=False)

    work = await work_store.list_items(run_id=run_id)
    active = [item for item in work if item.status in ACTIVE_STATUSES]
    integrity = await graph.integrity_report()
    cleanup = await graph.cleanup_report()
    blockers: list[str] = []
    if active:
        blockers.append(f"{len(active)} active work item(s) have no runnable owner")
    blockers.extend(
        f"unresolved hypothesis: {item}" for item in integrity.unresolved_hypotheses
    )
    blockers.extend(
        f"outstanding cleanup: {item.node_id}" for item in cleanup.outstanding
    )

    if active:
        reason = TerminationReason.INCOMPLETE_ACTIVE_WORK
    elif cleanup.outstanding:
        reason = TerminationReason.CLEANUP_REQUIRED
    elif integrity.unresolved_hypotheses:
        reason = TerminationReason.UNRESOLVED_HYPOTHESES
    else:
        return ConvergenceDecision(
            terminal=True,
            phase=AgentPhase.FINISHED,
            reason=no_agent_reason,
        )

    return ConvergenceDecision(
        terminal=True,
        phase=AgentPhase.FAILED,
        reason=reason,
        blockers=blockers,
        active_work_count=len(active),
        unresolved_hypothesis_count=len(integrity.unresolved_hypotheses),
        outstanding_cleanup_count=len(cleanup.outstanding),
    )
