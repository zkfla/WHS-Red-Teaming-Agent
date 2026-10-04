"""LangGraph-facing state.

``RuntimeState`` is the durable slice: ids and counters only
(instruction.md section 5). Domain state lives in the Attack Graph and
the Evidence Store and is referenced by id. ``GraphState`` adds transient
per-turn hand-offs that never leave this subpackage.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, TypedDict

from pydantic import BaseModel, Field, field_validator

from redteam.attack_graph.observation import GraphObservation
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.policy import PolicyDecision
from redteam.core.contracts.tool import ToolResult
from redteam.core.enums import AgentPhase, MissionStatus, Verdict
from redteam.core.ids import (
    ActionId,
    AgentId,
    ApprovalId,
    EngagementId,
    EventId,
    EvidenceId,
    HypothesisId,
    MissionId,
    OperatorIntentId,
    RunId,
    TaskId,
)
from redteam.orchestration.agents.base import PlanStep
from redteam.orchestration.component_binding import RuntimeComponentBinding
from redteam.orchestration.delegation import DelegationState
from redteam.orchestration.diagnostics import DecisionDiagnostic, safe_feedback
from redteam.orchestration.dispatch import (
    DispatchItem,
    WorkerOutcome,
    merge_worker_outcomes,
)
from redteam.orchestration.runtime_observation import RuntimeObservation


class TerminationReason:
    """Why a run stopped. Every exit path names itself."""

    NO_ACTIONABLE_PLAN = "no_actionable_plan"
    MISSION_BLOCKED = "mission_blocked"
    MAX_STEPS_REACHED = "max_steps_reached"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CONSECUTIVE_FAILURES = "consecutive_failures"
    STALLED = "stalled_no_graph_progress"
    INCOMPLETE_ACTIVE_WORK = "incomplete_active_work"
    UNRESOLVED_HYPOTHESES = "unresolved_hypotheses"
    CLEANUP_REQUIRED = "cleanup_required"
    NO_AGENTS = "no_agents_registered"
    APPROVAL_PENDING = "approval_pending"
    USER_INTERRUPTED = "user_interrupted"
    EXECUTION_ERROR = "execution_error"
    PLANNING_ERROR = "planning_error"
    GOAL_ACHIEVED = "goal_achieved"
    INVESTIGATION_HELD = "investigation_held"
    #: Operator asked to hold the run before the next action executes. The
    #: active Task and Mission are preserved so a RESUME intent continues
    #: the same work instead of starting a new Mission.
    OPERATOR_PAUSED = "operator_paused"
    #: Operator asked to end the Mission. Never resumed automatically.
    OPERATOR_STOPPED = "operator_stopped"


class RuntimeState(BaseModel):
    run_id: RunId
    engagement_id: EngagementId

    phase: AgentPhase = AgentPhase.OBSERVE
    current_agent: AgentId | None = None

    current_goal: str | None = None
    # Additive references for task-first execution. Optional defaults keep
    # checkpoints created before Mission contracts loadable.
    mission_id: MissionId | None = None
    active_task_id: TaskId | None = None
    active_path_id: str | None = None
    active_path_version: int | None = None
    active_path_step_id: str | None = None
    delegations: dict[str, DelegationState] = Field(default_factory=dict)
    planning_error: str | None = None
    pending_event_count: int = Field(default=0, ge=0)
    event_delivery_pending: bool = False
    planning_diagnostic: DecisionDiagnostic | None = None
    planning_diagnostic_history: list[DecisionDiagnostic] = Field(default_factory=list)
    planning_feedback: list[str] = Field(default_factory=list)
    model_requests_used: int = Field(default=0, ge=0)
    model_tokens_used: int = Field(default=0, ge=0)
    model_usage_unknown: int = Field(default=0, ge=0)
    unresolved_evidence_ids: list[str] = Field(default_factory=list)
    evidence_reads: list[dict] = Field(default_factory=list)
    local_tool_result_ids: list[str] = Field(default_factory=list)
    recent_observations: list[RuntimeObservation] = Field(default_factory=list)
    operator_intent_id: OperatorIntentId | None = None
    mission_status: MissionStatus | None = None

    active_hypothesis_id: HypothesisId | None = None
    pending_action_id: ActionId | None = None
    pending_approval_id: ApprovalId | None = None

    retry_count: int = 0
    step_count: int = 0

    remaining_budget: int | None = None
    # Wall-clock start of a run for restoring duration limits after restart.
    # Optional for checkpoints written before duration accounting existed.
    started_at: datetime | None = None

    last_verdict: Verdict | None = None

    # -- re-plan bookkeeping (execution state only) -------------------

    #: Set once, when the loop exits. See ``TerminationReason``.
    termination_reason: str | None = None
    actions_executed: int = 0
    # Durable count of additional tool calls made by bounded retries. The
    # Control Plane restores its tool-call budget from this on restart.
    retry_tool_calls: int = Field(default=0, ge=0)
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
    #: Which registered component set this run was built from. None is legacy.
    component_binding: RuntimeComponentBinding | None = None

    @field_validator("planning_feedback", mode="before")
    @classmethod
    def _safe_feedback(cls, values: list[str]) -> list[str]:
        return [safe_feedback(value) for value in values[-10:]]

    @field_validator("planning_diagnostic_history", mode="before")
    @classmethod
    def _bounded_diagnostic_history(
        cls, values: list[DecisionDiagnostic]
    ) -> list[DecisionDiagnostic]:
        return values[-10:]

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
    result_evidence_id: EvidenceId | None
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


def initial_graph_state(
    runtime: RuntimeState, *, last_event_id: EventId | None = None
) -> GraphState:
    """A ``GraphState`` with every transient field explicitly empty."""
    return {
        "runtime": runtime,
        "observation": None,
        "plan_step": None,
        "action_proposal": None,
        "policy_decision": None,
        "tool_result": None,
        "result_evidence_id": None,
        "current_work_id": None,
        "dispatch_items": [],
        "worker_outcomes": {},
        "last_event_id": last_event_id,
    }


def resume_graph_state(
    runtime: RuntimeState,
    *,
    action_proposal: ActionProposal,
    plan_step: PlanStep | None,
    current_work_id: str | None = None,
) -> GraphState:
    """Seed a resume invocation that re-enters directly at POLICY.

    Used only by ``AgentLoopRunner.reconcile_once`` to resume a run that
    stopped at ``APPROVAL_PENDING``, once the Approval has been resolved —
    with the exact ``ActionProposal``/``PlanStep`` the original PLAN turn
    already produced (preserved by ``replan_node``'s terminal branch),
    never a freshly re-planned one. ``runtime.phase`` must already be
    ``AgentPhase.POLICY`` (the graph's conditional entry edge — see
    ``orchestration.graph``) and ``runtime.pending_action_id`` must match
    ``action_proposal.action_id`` (``policy_node`` asserts this to decide
    whether to ask Control for a fresh judgement or the resolved outcome).
    """
    dispatch_items = (
        [
            DispatchItem(
                work_id=current_work_id, plan_step=plan_step, proposal=action_proposal
            )
        ]
        if plan_step is not None and plan_step.delegation_id and current_work_id is not None
        else []
    )
    return {
        "runtime": runtime,
        "observation": None,
        "plan_step": plan_step,
        "action_proposal": action_proposal,
        "policy_decision": None,
        "tool_result": None,
        "result_evidence_id": None,
        "current_work_id": current_work_id,
        "dispatch_items": dispatch_items,
        "worker_outcomes": {},
        "last_event_id": None,
    }
