"""Bounded delegation decisions for autonomous investigation (worker E).

Plan 3.3 and 3.5.5. The root drives the upper graph per-call resume
loop: it follows the checkpointed active delegation, gates with this
helper, asks the model through its existing ask path with the payload
built here, then routes proposed follow-up actions through the same
compile/scheduler/dispatch path as initial actions. This module holds
no model client, runs no tool loop, opens no nested subgraph, and
never declares overall-goal completion.

Budget rules implemented here:

- Real delegation allowance comes from the existing work budget as
  min(global remaining, delegation remaining) and is never re-issued
  per proposal.
- The separate decision limit is consumed cumulatively, keeping one
  spare request for the final cleanup summary. Request-bundle retries
  owned by the caller are reported back as attempts consumed so the
  two counters are never mixed.
- Time is measured from the stored start instant plus elapsed time.
- Tokens use measured per-request usage supplied by the caller through
  the metered inference path owned by F1/root. When the delegation
  sets a token cap and usage is unknown, further model investigation
  stops and the helper returns the stored partial results with the
  reason.
- After stop, forced cancel, or deadline, and whenever no allowance
  remains, the helper returns stored records only and the model is
  never called.
- Cleanup returns stored evidence references, failures, unresolved
  items, and the termination reason. It never proposes new actions.

Target references are opaque here. Entries supplied by the closed-loop
mapper travel through the next-action verdict untouched for the
root-owned reference validation/registration path. This module parses
no raw payloads, joins no addresses, and compares no raw locations:
raw handling lives entirely with root and D.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from redteam.core.ids import new_id
from redteam.orchestration.delegation import DelegationRequest, DelegationState
from redteam.orchestration.work.models import WorkBudget

# Scoped model-call stage shared by the engine, this helper, and review.
# One literal, never a per-role variant.
WORKER_NEXT_ACTION_STAGE = "worker_next_action"

# Delegation-scoped termination reasons. They describe the investigation
# only; overall-goal completion is declared by the root, never here.
TERMINATION_OBJECTIVE_DONE = "objective_done"
TERMINATION_NO_FURTHER_ACTION = "no_further_action"
TERMINATION_ACTIONS_EXHAUSTED = "actions_exhausted"
TERMINATION_DECISION_BUDGET_EXHAUSTED = "decision_budget_exhausted"
TERMINATION_DECISION_BUDGET_RESERVED = "decision_budget_reserved_for_cleanup"
TERMINATION_TOKEN_EXHAUSTED = "token_exhausted"
TERMINATION_TOKEN_USAGE_UNKNOWN = "token_usage_unknown"
TERMINATION_TIME_EXHAUSTED = "time_exhausted"
TERMINATION_STOPPED = "stopped"
TERMINATION_DEADLINE_EXCEEDED = "deadline_exceeded"
TERMINATION_APPROVAL_HELD = "approval_pending"
TERMINATION_WORKER_HELD = "worker_held"
TERMINATION_ALREADY_FINISHED = "already_finished"

INVESTIGATE = "investigate"
CLEANUP = "cleanup"
HELD = "held"

DECISION_PROPOSE_ACTIONS = "propose_actions"
DECISION_READ_EVIDENCE = "read_evidence"
DECISION_FINISH = "finish"
DECISION_HOLD = "hold"


@dataclass(frozen=True)
class DelegationGateInput:
    """Everything the pure gate needs; no stores, no model, no clock I/O."""

    delegation: DelegationState
    global_remaining_actions: int | None = None
    decision_remaining: int | None = None
    now: datetime | None = None
    stop_requested: bool = False
    deadline_exceeded: bool = False
    approval_pending: bool = False
    usage_unknown: bool = False


@dataclass(frozen=True)
class DelegationGate:
    """One turn permission: investigate, finish from stored records, or wait."""

    action: str
    reason: str
    investigate_model_allowed: bool
    cleanup_model_allowed: bool
    remaining_actions: int | None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _elapsed_seconds(delegation: DelegationState, now: datetime) -> float:
    started = delegation.started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    moment = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    return max((moment - started).total_seconds(), 0.0)


def remaining_actions(
    delegation: DelegationState, global_remaining_actions: int | None = None
) -> int:
    """Effective action allowance: the smaller of delegation and global."""

    own = max(delegation.budget.max_actions - max(delegation.actions_used, 0), 0)
    if global_remaining_actions is None:
        return own
    return max(min(own, max(global_remaining_actions, 0)), 0)


def remaining_decisions(delegation: DelegationState) -> int:
    """Lifetime model-call allowance left for this delegation."""

    return max(delegation.decision_limit - max(delegation.decision_attempts_used, 0), 0)


def gate_delegation_decision(gate_in: DelegationGateInput) -> DelegationGate:
    """Pure bounded gate: may this turn spend a model call on investigation?

    Never touches the model or any store. Cleanup means: return stored
    records only; at most one model call may still be used for the final
    summary when cleanup_model_allowed is true, and none otherwise.
    """

    delegation = gate_in.delegation
    now = gate_in.now or _utcnow()
    actions_left = remaining_actions(delegation, gate_in.global_remaining_actions)
    if gate_in.decision_remaining is not None:
        decisions_left = max(gate_in.decision_remaining, 0)
    else:
        decisions_left = remaining_decisions(delegation)

    if delegation.status == "finished":
        return DelegationGate(
            action=CLEANUP,
            reason=TERMINATION_ALREADY_FINISHED,
            investigate_model_allowed=False,
            cleanup_model_allowed=False,
            remaining_actions=actions_left,
        )
    if gate_in.stop_requested:
        return DelegationGate(
            action=CLEANUP,
            reason=TERMINATION_STOPPED,
            investigate_model_allowed=False,
            cleanup_model_allowed=False,
            remaining_actions=actions_left,
        )
    if gate_in.deadline_exceeded:
        return DelegationGate(
            action=CLEANUP,
            reason=TERMINATION_DEADLINE_EXCEEDED,
            investigate_model_allowed=False,
            cleanup_model_allowed=False,
            remaining_actions=actions_left,
        )
    if gate_in.approval_pending or delegation.status == "held":
        return DelegationGate(
            action=HELD,
            reason=TERMINATION_APPROVAL_HELD,
            investigate_model_allowed=False,
            cleanup_model_allowed=False,
            remaining_actions=actions_left,
        )
    max_seconds = delegation.budget.max_seconds
    if max_seconds is not None and _elapsed_seconds(delegation, now) >= max_seconds:
        return DelegationGate(
            action=CLEANUP,
            reason=TERMINATION_TIME_EXHAUSTED,
            investigate_model_allowed=False,
            cleanup_model_allowed=False,
            remaining_actions=actions_left,
        )
    if actions_left <= 0:
        return DelegationGate(
            action=CLEANUP,
            reason=TERMINATION_ACTIONS_EXHAUSTED,
            investigate_model_allowed=False,
            cleanup_model_allowed=decisions_left >= 1,
            remaining_actions=actions_left,
        )
    max_tokens = delegation.budget.max_tokens
    if max_tokens is not None:
        if delegation.tokens_used is None or gate_in.usage_unknown:
            return DelegationGate(
                action=CLEANUP,
                reason=TERMINATION_TOKEN_USAGE_UNKNOWN,
                investigate_model_allowed=False,
                cleanup_model_allowed=False,
                remaining_actions=actions_left,
            )
        if delegation.tokens_used >= max_tokens:
            return DelegationGate(
                action=CLEANUP,
                reason=TERMINATION_TOKEN_EXHAUSTED,
                investigate_model_allowed=False,
                cleanup_model_allowed=False,
                remaining_actions=actions_left,
            )
    if decisions_left <= 0:
        return DelegationGate(
            action=CLEANUP,
            reason=TERMINATION_DECISION_BUDGET_EXHAUSTED,
            investigate_model_allowed=False,
            cleanup_model_allowed=False,
            remaining_actions=actions_left,
        )
    if decisions_left == 1:
        return DelegationGate(
            action=CLEANUP,
            reason=TERMINATION_DECISION_BUDGET_RESERVED,
            investigate_model_allowed=False,
            cleanup_model_allowed=True,
            remaining_actions=actions_left,
        )
    return DelegationGate(
        action=INVESTIGATE,
        reason="ready",
        investigate_model_allowed=True,
        cleanup_model_allowed=True,
        remaining_actions=actions_left,
    )

def open_delegation(
    request: DelegationRequest,
    *,
    path_id: str,
    path_version: int,
    mission_task_id: str | None = None,
    decision_limit: int,
    started_at: datetime | None = None,
    delegation_id: str | None = None,
) -> DelegationState:
    """Bind a root proposal to the current mission/task/path/step context.

    Called by the root when it accepts a delegation proposal. Identity
    and default limits belong to the runtime, so the model never
    supplies them.
    """

    if not (request.objective or "").strip():
        raise ValueError("delegation needs an objective")
    return DelegationState(
        delegation_id=delegation_id or new_id("delegation"),
        agent_id=request.agent_id,
        objective=request.objective or "",
        path_id=path_id,
        path_version=path_version,
        path_step_id=request.path_step_id,
        mission_task_id=mission_task_id,
        budget=request.budget or WorkBudget(),
        decision_limit=decision_limit,
        started_at=started_at or _utcnow(),
    )


def build_worker_next_action_payload(
    *,
    delegation: DelegationState,
    evidence_ids: Sequence[str] = (),
    evidence_excerpts: Sequence[Mapping[str, Any]] = (),
    unresolved: Sequence[str] = (),
    remaining_actions: int | None = None,
    target_references: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Scoped model input: purpose, scope, remaining allowance, pointers.

    Carries evidence ids and caller-prepared excerpts only; raw bodies
    are never copied into the delegation or the payload. Reference
    entries pass through untouched for root-owned validation.
    """

    return {
        "stage": WORKER_NEXT_ACTION_STAGE,
        "delegation_id": delegation.delegation_id,
        "agent_id": str(delegation.agent_id),
        "objective": delegation.objective,
        "path_id": delegation.path_id,
        "path_version": delegation.path_version,
        "path_step_id": delegation.path_step_id,
        "mission_task_id": delegation.mission_task_id,
        "remaining_actions": remaining_actions,
        "evidence_ids": list(evidence_ids),
        "evidence_excerpts": list(evidence_excerpts),
        "unresolved": list(unresolved),
        "target_references": list(target_references),
    }


def worker_next_action_schema() -> dict[str, Any]:
    """Response contract for the scoped worker_next_action call.

    The root passes this as the response schema of its existing ask
    path. Hold carries no actions: approvals always resolve upstream.
    """

    return {
        "type": "object",
        "properties": {
            "decision": {
                "type": "string",
                "enum": [
                    DECISION_PROPOSE_ACTIONS,
                    DECISION_READ_EVIDENCE,
                    DECISION_FINISH,
                    DECISION_HOLD,
                ],
            },
            "actions": {"type": "array", "items": {"type": "object"}},
            "evidence_ids": {"type": "array", "items": {"type": "string"}},
            "target_references": {"type": "array", "items": {"type": "object"}},
            "summary": {"type": "string"},
            "unresolved": {"type": "array", "items": {"type": "string"}},
            "finish_reason": {"type": "string"},
        },
        "required": ["decision"],
        "additionalProperties": True,
    }


@dataclass(frozen=True)
class WorkerNextAction:
    """One scoped model verdict: act, read, finish, or wait. Never a goal claim."""

    decision: str
    actions: tuple[dict[str, Any], ...] = ()
    evidence_ids: tuple[str, ...] = ()
    target_references: tuple[Mapping[str, Any], ...] = ()
    summary: str | None = None
    unresolved: tuple[str, ...] = ()
    finish_reason: str | None = None


@dataclass(frozen=True)
class DelegationFinish:
    """Stored-records return for the root. Never carries new tool actions."""

    delegation_id: str
    reason: str
    summary: str | None
    evidence_ids: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    new_tool_actions: tuple[dict[str, Any], ...] = ()
    call_model: bool = False


@dataclass(frozen=True)
class TurnConsumption:
    """Measured cost of one scoped call, reported back by the caller.

    Attempts is the actual model-request count the caller measured
    (its retry-budget delta, defaulting to one) so lifetime and
    per-call counters are never mixed. Tokens is the measured total,
    or None when the provider reported no usage.
    """

    attempts: int = 1
    tokens: int | None = None
    usage_unknown: bool = False


@dataclass(frozen=True)
class DelegationDecisionResult:
    """Outcome of one delegation turn: updated state plus one verdict."""

    delegation: DelegationState
    gate: DelegationGate
    next_action: WorkerNextAction | None = None
    finish: DelegationFinish | None = None
    usage_unknown: bool = False
    error: str | None = None


def _string_items(value: Any, *, name: str, required: bool) -> tuple[str, ...]:
    if value is None:
        value = []
    if not isinstance(value, list) or (required and not value):
        raise ValueError("worker_next_action " + name + " needs a string array")
    if any(not isinstance(item, str) for item in value):
        raise ValueError("worker_next_action " + name + " holds strings only")
    return tuple(value)


def parse_worker_next_action(result: Mapping[str, Any]) -> WorkerNextAction:
    """Validate one scoped verdict. Opaque action and reference entries
    pass through: the action compiler owns action checks and the
    root-owned reference registration owns reference checks."""

    if not isinstance(result, Mapping):
        raise ValueError("worker_next_action must be a JSON object")
    decision = result.get("decision")
    references = result.get("target_references", [])
    if references is None:
        references = []
    if not isinstance(references, list):
        raise ValueError("worker_next_action target_references needs an array")
    if decision == DECISION_PROPOSE_ACTIONS:
        actions = result.get("actions")
        if not isinstance(actions, list) or not actions:
            raise ValueError("worker_next_action propose_actions needs actions")
        if any(not isinstance(item, dict) for item in actions):
            raise ValueError("worker_next_action actions holds objects only")
        return WorkerNextAction(
            decision=decision,
            actions=tuple(actions),
            evidence_ids=_string_items(result.get("evidence_ids", []), name="evidence_ids", required=False),
            target_references=tuple(references),
        )
    if decision == DECISION_READ_EVIDENCE:
        return WorkerNextAction(
            decision=decision,
            evidence_ids=_string_items(result.get("evidence_ids"), name="evidence_ids", required=True),
            target_references=tuple(references),
        )
    if decision == DECISION_FINISH:
        summary = result.get("summary", "")
        if not isinstance(summary, str):
            raise ValueError("worker_next_action finish needs a string summary")
        finish_reason = result.get("finish_reason", TERMINATION_OBJECTIVE_DONE)
        if not isinstance(finish_reason, str) or not finish_reason:
            raise ValueError("worker_next_action finish needs a finish_reason")
        return WorkerNextAction(
            decision=decision,
            evidence_ids=_string_items(result.get("evidence_ids", []), name="evidence_ids", required=False),
            target_references=tuple(references),
            summary=summary,
            unresolved=_string_items(result.get("unresolved", []), name="unresolved", required=False),
            finish_reason=finish_reason,
        )
    if decision == DECISION_HOLD:
        summary = result.get("summary", "")
        if not isinstance(summary, str):
            raise ValueError("worker_next_action hold needs a string summary")
        return WorkerNextAction(
            decision=decision,
            evidence_ids=_string_items(result.get("evidence_ids", []), name="evidence_ids", required=False),
            target_references=tuple(references),
            summary=summary,
            unresolved=_string_items(result.get("unresolved", []), name="unresolved", required=False),
        )
    raise ValueError("worker_next_action decision is not a known verdict")


def record_decision_consumption(
    delegation: DelegationState, *, requests: int = 1, tokens: int | None = None
) -> DelegationState:
    """Fold one scoped call measured cost into the delegation.

    Unknown usage never fabricates a zero: tokens None writes a sticky
    unknown that later known usage does not clear, so a restart keeps
    the unknown stop instead of losing it.
    """

    update: dict[str, Any] = {
        "decision_attempts_used": max(delegation.decision_attempts_used, 0) + max(requests, 0),
    }
    if tokens is None:
        update["tokens_used"] = None
    elif delegation.tokens_used is not None:
        update["tokens_used"] = delegation.tokens_used + max(tokens, 0)
    return delegation.model_copy(update=update)


def mark_held(
    delegation: DelegationState,
    *,
    reason: str = TERMINATION_APPROVAL_HELD,
    summary: str | None = None,
    unresolved: Sequence[str] | None = None,
) -> DelegationState:
    """Park an active delegation: approvals wait upstream, model holds wait on purpose."""

    if delegation.status != "active":
        return delegation
    update: dict[str, Any] = {"status": "held", "termination_reason": reason}
    if summary is not None:
        update["summary"] = summary
    if unresolved is not None:
        update["unresolved"] = list(unresolved)
    return delegation.model_copy(update=update)



def mark_active(delegation: DelegationState) -> DelegationState:
    """Return a held delegation to its purpose after approval resumes.

    The same delegation id, purpose, and remaining allowance continue;
    nothing is re-issued or re-planned by this transition.
    """

    if delegation.status != "held":
        return delegation
    return delegation.model_copy(update={"status": "active", "termination_reason": None})


def mark_finished(
    delegation: DelegationState,
    *,
    reason: str,
    summary: str | None = None,
    unresolved: Sequence[str] | None = None,
) -> DelegationState:
    """Close the investigation. Finished state is never rewritten."""

    if delegation.status == "finished":
        return delegation
    update: dict[str, Any] = {"status": "finished", "termination_reason": reason}
    if summary is not None:
        update["summary"] = summary
    if unresolved is not None:
        update["unresolved"] = list(unresolved)
    return delegation.model_copy(update=update)


def build_cleanup_finish(delegation: DelegationState, *, reason: str) -> DelegationFinish:
    """Stored-records return: evidence, failures, unresolved, and reason.

    Carries no tool actions and requests no model call by construction.
    """

    return DelegationFinish(
        delegation_id=delegation.delegation_id,
        reason=reason,
        summary=delegation.summary,
        evidence_ids=tuple(delegation.evidence_ids),
        unresolved=tuple(delegation.unresolved),
    )


def close_from_gate(delegation: DelegationState, gate: DelegationGate) -> tuple[DelegationState, DelegationFinish | None]:
    """Durable end for a non-investigate gate: hold parks, cleanup closes.

    The root calls this when the gate forbids a model call, so the
    checkpoint carries the terminal state without any new request.
    """

    if gate.action == HELD:
        return mark_held(delegation), None
    closed = mark_finished(delegation, reason=gate.reason)
    return closed, build_cleanup_finish(closed, reason=gate.reason)


def apply_turn_result(
    *,
    delegation: DelegationState,
    gate: DelegationGate,
    verdict: WorkerNextAction | None = None,
    error: str | None = None,
    consumption: TurnConsumption | None = None,
) -> DelegationDecisionResult:
    """Bookkeep one scoped call the root already made through its ask path.

    The root gates, asks, parses, then hands the outcome here: cost is
    folded once, finish and hold transitions are applied, and the
    unknown-usage flag is raised for the next gate. Parse errors keep
    the spent cost so the caller can retry inside the allowance as a
    format failure.
    """

    measured = consumption or TurnConsumption()
    unknown_now = bool(
        (measured.tokens is None and delegation.budget.max_tokens is not None)
        or measured.usage_unknown
    )
    spent = record_decision_consumption(
        delegation, requests=measured.attempts, tokens=measured.tokens
    )
    if error is not None:
        return DelegationDecisionResult(
            delegation=spent,
            gate=gate,
            usage_unknown=unknown_now,
            error=error[:300],
        )
    if verdict is None:
        return DelegationDecisionResult(
            delegation=spent,
            gate=gate,
            usage_unknown=unknown_now,
            error="worker_next_action returned no verdict",
        )
    if verdict.decision == DECISION_HOLD:
        parked = mark_held(
            spent,
            reason=TERMINATION_WORKER_HELD,
            summary=verdict.summary,
            unresolved=list(verdict.unresolved) or None,
        )
        return DelegationDecisionResult(
            delegation=parked,
            gate=gate,
            next_action=verdict,
            usage_unknown=unknown_now,
        )
    if verdict.decision == DECISION_FINISH:
        closed = mark_finished(
            spent,
            reason=verdict.finish_reason or TERMINATION_OBJECTIVE_DONE,
            summary=verdict.summary,
            unresolved=list(verdict.unresolved),
        )
        return DelegationDecisionResult(
            delegation=closed,
            gate=gate,
            next_action=verdict,
            usage_unknown=unknown_now,
        )
    return DelegationDecisionResult(
        delegation=spent,
        gate=gate,
        next_action=verdict,
        usage_unknown=unknown_now,
    )
