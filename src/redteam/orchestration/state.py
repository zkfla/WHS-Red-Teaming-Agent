"""LangGraph-facing state.

``RuntimeState`` is the durable slice: ids and counters only
(instruction.md section 5). Domain state lives in the Attack Graph and
the Evidence Store and is referenced by id. ``GraphState`` adds transient
per-turn hand-offs that never leave this subpackage.
"""

from __future__ import annotations

from typing import TypedDict

from pydantic import BaseModel, Field

from redteam.attack_graph.observation import GraphObservation
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.policy import PolicyDecision
from redteam.core.contracts.tool import ToolResult
from redteam.core.enums import AgentPhase, Verdict
from redteam.core.ids import ActionId, AgentId, EngagementId, EventId, HypothesisId, RunId
from redteam.orchestration.agents.base import PlanStep


class TerminationReason:
    """Why a run stopped. Every exit path names itself."""

    NO_ACTIONABLE_PLAN = "no_actionable_plan"
    MAX_STEPS_REACHED = "max_steps_reached"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CONSECUTIVE_FAILURES = "consecutive_failures"
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
    consecutive_failures: int = 0
    #: Agents that have run out of proposable steps, in finish order.
    finished_agents: list[AgentId] = Field(default_factory=list)

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
        "last_event_id": None,
    }
