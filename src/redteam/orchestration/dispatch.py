"""Contracts for bounded LangGraph map/reduce action dispatch.

The same envelope also carries delegated follow-up actions (plan 3.5.5):
one entry per follow-up with its parent linkage, candidate identity, and
stored evidence references. Aggregation widens its inputs to these entries
without changing its formulas. All delegation fields are optional, so rows
written before delegation existed keep loading and merging unchanged.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from redteam.core.contracts.action import ActionProposal
from redteam.core.enums import PolicyVerdict, Verdict
from redteam.core.ids import ApprovalId, EventId
from redteam.orchestration.agents.base import PlanStep
from redteam.orchestration.runtime_observation import RuntimeObservation


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
    # Follow-up linkage for delegated investigation. None on the initial
    # dispatch batch; set on each later action the delegation chose.
    parent_action_id: str | None = None
    # Stable execution identity of the follow-up candidate, for dedup and
    # audit. Empty on rows written before delegation existed.
    candidate_key: str = ""
    # Stored evidence produced by this action (ids only, never bodies).
    # Bodies stay in the evidence store and are read back by id.
    evidence_ids: list[str] = Field(default_factory=list)
    # Delegation that chose this action, if any. Entries whose id is
    # unknown to the runtime update global counters only; they never
    # create delegation state on their own.
    delegation_id: str | None = None
    # Acting role identity for per-role counting of follow-up actions.
    # Empty on rows written by paths that count roles from dispatch.
    agent_id: str = ""
    executed: bool = False
    retry_attempts: int = 0
    # Actual analysis-model requests made for this action, including retries.
    # Aggregation adds these deltas once to the owning delegation; the
    # earlier planning requests have already been accounted for there.
    model_decision_attempts: int = Field(default=0, ge=0)
    # Unknown request usage is None and stays sticky in delegation totals.
    # Observation skips, code-only reviews and guards before a call cost 0.
    model_tokens_used: int | None = Field(default=0, ge=0)
    model_known_tokens_used: int = Field(default=0, ge=0)
    model_usage_unknown: int = Field(default=0, ge=0)
    succeeded: bool | None = None
    policy_verdict: PolicyVerdict | None = None
    policy_severity: str | None = None
    policy_reason: str | None = None
    policy_budget_abort: bool = False
    approval_id: ApprovalId | None = None
    verdict: Verdict | None = None
    last_event_id: EventId | None = None
    error: str | None = None
    runtime_observations: list[RuntimeObservation] = Field(default_factory=list)


def merge_worker_outcomes(
    left: dict[str, WorkerOutcome], right: dict[str, WorkerOutcome]
) -> dict[str, WorkerOutcome]:
    """Merge parallel writes by action id; replay of the same id is idempotent."""

    return {**left, **right}


def is_delegated_follow_up(outcome: WorkerOutcome) -> bool:
    """Whether this envelope entry is a delegated follow-up action."""

    return outcome.delegation_id is not None or outcome.parent_action_id is not None
