"""Contracts for bounded LangGraph map/reduce action dispatch."""

from __future__ import annotations

from pydantic import BaseModel

from redteam.core.contracts.action import ActionProposal
from redteam.core.enums import PolicyVerdict, Verdict
from redteam.core.ids import EventId
from redteam.orchestration.agents.base import PlanStep


class DispatchItem(BaseModel):
    """One leased action sent to an isolated worker state."""

    work_id: str
    plan_step: PlanStep
    proposal: ActionProposal
    last_event_id: EventId | None = None


class WorkerOutcome(BaseModel):
    """Small commutative result folded back into the coordinator state."""

    action_id: str
    work_id: str
    executed: bool = False
    succeeded: bool | None = None
    policy_verdict: PolicyVerdict | None = None
    verdict: Verdict | None = None
    last_event_id: EventId | None = None
    error: str | None = None


def merge_worker_outcomes(
    left: dict[str, WorkerOutcome], right: dict[str, WorkerOutcome]
) -> dict[str, WorkerOutcome]:
    """Merge parallel writes by action id; replay of the same id is idempotent."""

    return {**left, **right}
