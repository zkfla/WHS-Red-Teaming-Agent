"""LangGraph-facing state.

``RuntimeState`` is the durable slice: ids and counters only
(instruction.md section 5). Domain state lives in the Attack Graph and
the Evidence Store and is referenced by id. ``GraphState`` adds transient
per-turn hand-offs that never leave this subpackage.
"""

from __future__ import annotations

from typing import Annotated, TypedDict

from pydantic import BaseModel, Field

from redteam.attack_graph.observation import GraphObservation
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.policy import PolicyDecision
from redteam.core.contracts.tool import ToolResult
from redteam.core.enums import AgentPhase, Verdict
from redteam.core.ids import ActionId, AgentId, EngagementId, EventId, HypothesisId, RunId
from redteam.orchestration.agents.base import PlanStep
from redteam.orchestration.dispatch import (
    DispatchItem,
    WorkerOutcome,
    merge_worker_outcomes,
)


class TerminationReason:
    """Why a run stopped. Every exit path names itself."""

    NO_ACTIONABLE_PLAN = "no_actionable_plan"
    MAX_STEPS_REACHED = "max_steps_reached"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CONSECUTIVE_FAILURES = "consecutive_failures"
    STALLED = "stalled_no_graph_progress"
    INCOMPLETE_ACTIVE_WORK = "incomplete_active_work"
    UNRESOLVED_HYPOTHESES = "unresolved_hypotheses"
    CLEANUP_REQUIRED = "cleanup_required"
    NO_AGENTS = "no_agents_registered"


class RuntimeState(BaseModel):
    run_id: RunId
    engagement_id: EngagementId

    phase: AgentPhase = AgentPhase.OBSERVE
    current_agent: AgentId | None = None

    current_goal: str | None = None

    active_hypothesis_id: HypothesisId | None = None
    pending_action_id: ActionId | None = None

    retry_count: int = 0
    step_count: int = 0

    remaining_budget: int | None = None

    last_verdict: Verdict | None = None

    # -- re-plan bookkeeping (execution state only) -------------------

    #: Set once, when the loop exits. See ``TerminationReason``.
    termination_reason: str | None = None
    actions_executed: int = 0
    actions_by_agent: dict[str, int] = Field(default_factory=dict)
    consecutive_failures: int = 0
    no_progress_turns: int = 0
    last_progress_fingerprint: str | None = None
    #: Agents that have run out of proposable steps, in finish order.
    finished_agents: list[AgentId] = Field(default_factory=list)
    #: Lifecycle event guard; rotations must not emit AgentStarted repeatedly.
    started_agents: list[AgentId] = Field(default_factory=list)
    #: Observation fingerprint at which each agent exhausted its frontier.
    #: A relevant graph delta makes that agent eligible again.
    agent_revision_cursors: dict[str, str] = Field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.phase in (AgentPhase.FINISHED, AgentPhase.FAILED)


class GraphState(TypedDict, total=False):
    runtime: RuntimeState

    # Transient only — never meaningful outside one turn of this subpackage.
    observation: GraphObservation | None
    plan_step: PlanStep | None
    action_proposal: ActionProposal | None
    policy_decision: PolicyDecision | None
    tool_result: ToolResult | None
    #: Internal WorkLedger reference for the in-flight proposal.
    current_work_id: str | None
    #: A batch exists only when ``max_actions_per_turn`` admits fan-out.
    dispatch_items: list[DispatchItem]
    #: Map/reduce channel. Entries are retained by action id for replay/audit.
    worker_outcomes: Annotated[dict[str, WorkerOutcome], merge_worker_outcomes]
    #: Present only in the isolated state passed to a worker via ``Send``.
    dispatch_item: DispatchItem
    #: Id of the last event published this turn, so the next node can
    #: cite it as ``causation_id`` and the chain stays walkable.
    last_event_id: EventId | None


def initial_graph_state(runtime: RuntimeState) -> GraphState:
    """A ``GraphState`` with every transient field explicitly empty."""
    return {
        "runtime": runtime,
        "observation": None,
        "plan_step": None,
        "action_proposal": None,
        "policy_decision": None,
        "tool_result": None,
        "current_work_id": None,
        "dispatch_items": [],
        "worker_outcomes": {},
        "last_event_id": None,
    }
