"""POLICY — hand the proposal to the Control Plane and nowhere else.

Makes no judgement of its own, including on REQUIRE_APPROVAL, which
routes away rather than waiting.

Resume exception: when ``runtime.pending_approval_id`` is already set and
matches this exact proposal (``AgentLoopRunner.reconcile_once`` seeded
this turn that way — see ``orchestration.state.resume_graph_state``),
this node must never ask Control for a *fresh* judgement — that would
mint a second Approval for an action a human already resolved. It asks
for the resolved *outcome* instead (``ControlPlane.approval_outcome``),
which itself never bypasses Control (see its docstring).
"""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.core.contracts.events import EventType
from redteam.core.contracts.policy import ControlContext, PolicyDecision
from redteam.core.contracts.target import target_display_name
from redteam.core.enums import AgentPhase, PolicyVerdict, Severity
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.diagnostics import operational_exception_diagnostic
from redteam.orchestration.engine import validate_current_action
from redteam.orchestration.nodes._support import advance, publisher_for
from redteam.orchestration.runtime_observation import (
    RuntimeObservation,
    append_runtime_observation,
)
from redteam.orchestration.state import GraphState, TerminationReason
from redteam.orchestration.work.models import WorkStatus

_OUTCOME = {
    PolicyVerdict.ALLOW: (EventType.ACTION_APPROVED, AgentPhase.EXECUTE),
    PolicyVerdict.REQUIRE_APPROVAL: (EventType.APPROVAL_REQUIRED, AgentPhase.REPLAN),
    PolicyVerdict.DENY: (EventType.ACTION_DENIED, AgentPhase.REPLAN),
}

#: The shared decision carries the reaction hint directly. Its compatibility
#: reader also understands legacy ``severity:<value>`` matched-rule tags.
SEVERITY_BLOCK_ACTION = Severity.BLOCK_ACTION.value
SEVERITY_REPLAN_GOAL = Severity.REPLAN_GOAL.value
SEVERITY_ABORT_RUN = Severity.ABORT_RUN.value


def denial_severity(decision: PolicyDecision | None) -> str | None:
    """Read the Control-supplied reaction hint out of a decision.

    Returns the raw level string (``"block_action"`` / ``"replan_goal"`` /
    ``"abort_run"``), or ``None`` when the decision is untagged or malformed.
    Untagged denials keep the historical behavior: block this action and
    replan.
    """

    return decision.denial_severity if decision is not None else None


async def policy_node(state: GraphState, *, deps: RuntimeDeps) -> GraphState:
    runtime = state["runtime"]
    proposal = state["action_proposal"]
    assert proposal is not None, "policy_node runs after a proposal was produced"

    correlation_id = str(proposal.action_id)
    publisher = publisher_for(state, deps, "orchestration.policy")

    problems = await validate_current_action(deps, runtime, state.get("plan_step"), proposal)
    if problems:
        decision = PolicyDecision(
            action_id=proposal.action_id,
            verdict=PolicyVerdict.DENY,
            reasons=problems,
            matched_rules=["orchestration:stale_or_invalid_path"],
            evaluated_at=datetime.now(timezone.utc),
        )
        event = await publisher.publish(
            EventType.ACTION_DENIED,
            {
                "action_id": proposal.action_id,
                "reasons": problems,
            },
            correlation_id=correlation_id,
            causation_id=state.get("last_event_id"),
        )
        rejected_runtime = append_runtime_observation(
            runtime.model_copy(
                update={
                    "phase": AgentPhase.REPLAN,
                    "pending_approval_id": None,
                    "termination_reason": None,
                    "planning_feedback": problems,
                }
            ),
            RuntimeObservation(
                kind="control",
                summary=(
                    f"Control DENY for {proposal.capability}.{proposal.operation}: "
                    + "; ".join(problems)
                )[:500],
                outcome=PolicyVerdict.DENY.value,
                step=runtime.step_count,
                action_id=str(proposal.action_id),
                capability=proposal.capability,
                operation=proposal.operation,
                target=(
                    target_display_name(proposal.target) if proposal.target is not None else None
                ),
                reasons=list(problems[:8]),
                matched_rules=["orchestration:stale_or_invalid_path"],
                alternative_allowed=True,
            ),
        )
        return advance(
            state, rejected_runtime, last_event_id=event.event_id, policy_decision=decision
        )

    # Live concurrency signal for ROE's max_parallel_actions (control/roe.py
    # cannot compute this itself: the Work Ledger is Orchestration's, not
    # Control's, to import). "Other" actions already leased/running for
    # this run, excluding the one currently being evaluated.
    current_work_id = state.get("current_work_id")
    in_flight = 0
    for status in (WorkStatus.LEASED, WorkStatus.RUNNING):
        in_flight += len(
            [
                item
                for item in await deps.work_store.list_items(status=status, run_id=runtime.run_id)
                if item.work_id != current_work_id
            ]
        )

    resuming_approval = (
        runtime.pending_approval_id is not None and runtime.pending_action_id == proposal.action_id
    )
    policy_diagnostic = None
    if resuming_approval:
        assert runtime.pending_approval_id is not None
        resume_error: str | None
        try:
            if proposal.binding_mode == "selected_tool":
                resolved = await deps.control_plane.approval_outcome(
                    runtime.pending_approval_id, current_proposal=proposal,
                )
            else:
                resolved = await deps.control_plane.approval_outcome(runtime.pending_approval_id)
        except Exception as exc:  # noqa: BLE001 - policy failures must fail closed
            resolved = None
            resume_error = type(exc).__name__
            policy_diagnostic = operational_exception_diagnostic(
                exc,
                stage="policy",
                code="approval_outcome_lookup_failed",
                summary="approval outcome lookup failed",
            )
        else:
            resume_error = None
        if resolved is not None:
            decision = resolved
        else:
            # Still pending (or the outcome lookup itself failed) --
            # never fabricate an ALLOW. Re-publish the same
            # REQUIRE_APPROVAL shape; the run ends up back at
            # APPROVAL_PENDING exactly as before this resume attempt.
            decision = PolicyDecision(
                action_id=proposal.action_id,
                verdict=PolicyVerdict.REQUIRE_APPROVAL,
                reasons=(
                    ["approval still pending"]
                    if resume_error is None
                    else [f"approval outcome lookup failed: {resume_error}"]
                ),
                approval_id=runtime.pending_approval_id,
                evaluated_at=datetime.now(timezone.utc),
            )
    else:
        context = ControlContext(
            engagement_id=runtime.engagement_id,
            agent_id=proposal.agent_id,
            correlation_id=correlation_id,
            in_flight_actions=in_flight,
            selected_tool_id=state["plan_step"].tool_id if state["plan_step"] is not None else None,
        )
        try:
            decision = await deps.control_plane.evaluate(proposal, context)
        except Exception as exc:  # noqa: BLE001 - policy failures must fail closed
            decision = PolicyDecision(
                action_id=proposal.action_id,
                verdict=PolicyVerdict.DENY,
                reasons=["control plane evaluation failed"],
                matched_rules=[f"control_error:{type(exc).__name__}"],
                evaluated_at=datetime.now(timezone.utc),
            )
            policy_diagnostic = operational_exception_diagnostic(
                exc,
                stage="policy",
                code="control_plane_evaluation_failed",
                summary="control plane evaluation failed",
            )

    evaluated = await publisher.publish(
        EventType.POLICY_EVALUATED,
        {
            "action_id": decision.action_id,
            "verdict": decision.verdict.value,
            "matched_rules": decision.matched_rules,
            "budget_remaining": decision.budget_remaining,
        },
        correlation_id=correlation_id,
        causation_id=state.get("last_event_id"),
    )

    event_type, next_phase = _OUTCOME[decision.verdict]
    outcome = await publisher.publish(
        event_type,
        {
            "action_id": decision.action_id,
            "reasons": decision.reasons,
            "approval_id": decision.approval_id,
        },
        correlation_id=correlation_id,
        causation_id=evaluated.event_id,
    )

    runtime_update: dict[str, object] = {"phase": next_phase}
    if decision.verdict is PolicyVerdict.REQUIRE_APPROVAL:
        runtime_update.update(
            {
                "termination_reason": TerminationReason.APPROVAL_PENDING,
                "pending_approval_id": decision.approval_id,
            }
        )
    elif resuming_approval:
        # The approval cycle is fully resolved now (ALLOW or DENY) --
        # clear the markers so a later, unrelated action is never
        # mistaken for a resume of this one. Also clear the terminal
        # ``APPROVAL_PENDING`` marker: it is no longer pending, and leaving
        # it set would make REPLAN re-enter its terminal branch (a rejected
        # approval would never replan, and an approved one would stop right
        # after executing). The run resumes/replans from the resolved verdict.
        runtime_update.update(
            {
                "pending_approval_id": None,
                "pending_action_id": None,
                "termination_reason": None,
            }
        )

    severity = denial_severity(decision)
    reason_text = ("; ".join(decision.reasons) or "no additional reason supplied")[:300]
    observed_runtime = append_runtime_observation(
        runtime.model_copy(update=runtime_update),
        RuntimeObservation(
            kind="control",
            summary=(
                f"Control {decision.verdict.value.upper()} for "
                f"{proposal.capability}.{proposal.operation}: {reason_text}"
            )[:500],
            outcome=decision.verdict.value,
            step=runtime.step_count,
            action_id=str(proposal.action_id),
            capability=proposal.capability,
            operation=proposal.operation,
            target=target_display_name(proposal.target) if proposal.target is not None else None,
            reasons=list(decision.reasons[:8]),
            matched_rules=list(decision.matched_rules[:12]),
            approval_id=(
                str(decision.approval_id)
                if decision.approval_id
                else (
                    str(runtime.pending_approval_id)
                    if resuming_approval and runtime.pending_approval_id
                    else None
                )
            ),
            requires_approval=decision.verdict is PolicyVerdict.REQUIRE_APPROVAL,
            alternative_allowed=(
                decision.verdict is PolicyVerdict.ALLOW or severity != SEVERITY_ABORT_RUN
            ),
            diagnostic=policy_diagnostic,
        ),
    )

    return advance(
        state,
        observed_runtime,
        last_event_id=outcome.event_id,
        policy_decision=decision,
    )
