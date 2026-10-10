"""Specialist judgments composed with fresh and reconciled native instances."""

from dataclasses import replace
from typing import Literal

from redteam.core.enums import AgentPhase
from redteam.orchestration.cognition import DecisionFailure
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.coordination import ChangeSet, CoordinationError, StateConflict
from redteam.orchestration.diagnostics import DecisionDiagnostic
from redteam.orchestration.instance_driver import InstanceDriver, InstanceEvent
from redteam.orchestration.metered_llm import llm_request_scope
from redteam.orchestration.outbox import pending_count
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.specialist_planner import SpecialistPlanner
from redteam.orchestration.state import RuntimeState, TerminationReason
from redteam.orchestration.task_activation import TaskActivation


class SpecialistLifecycle:
    """The same followup boundary for fresh start and verified cohort resume.

    Owns judgments, not runners, recovery, approvals or a second task queue.
    A partial/unknown cohort may return but cannot make the Mission quiescent.
    """
    def __init__(self, base: RuntimeState, deps: RuntimeDeps) -> None:
        if deps.specialist_policy is None:
            raise ValueError("specialist_policy_missing")
        self.base, self.deps = base, deps
        self.planner = SpecialistPlanner(deps)
        self.diagnostic: DecisionDiagnostic | None = None

    async def state(self, state: Literal["active", "held", "quiescent"]) -> None:
        async def set_state(tx: ChangeSet) -> None:
            run_id = str(self.base.run_id)
            outstanding = (TaskActivation.has_pending(tx, run_id) or pending_count(tx, run_id))
            tx.records[("specialist_lifecycle", run_id)] = {
                "state": "held" if state == "quiescent" and outstanding else state,
            }
        await self.planner.coordinator.change(set_state)

    async def propose(self, runtime: RuntimeState) -> None:
        try:
            with llm_request_scope(str(self.base.run_id), "specialist_planning"):
                await self.planner.advance(runtime)
        except DecisionFailure as exc:
            self.diagnostic = exc.diagnostic
        except (CoordinationError, StateConflict):
            # Stale plans do not change Tasks or erase completed work.
            # A new judgment needs genuinely new inputs, not an unbounded retry.
            self.diagnostic = DecisionDiagnostic(
                code="planning_context_changed", stage="specialist_admission", kind="stale",
                summary="Current planning inputs changed; no proposed Tasks were admitted.",
                retryable=False,
            )

    async def returned(self, event: InstanceEvent, *, allow_followups: bool = True) -> None:
        runtime = event.runtime
        if (allow_followups and event.kind == "completed" and runtime.actions_executed > 0
                and runtime.pending_approval_id is None
                and runtime.termination_reason not in {
                    TerminationReason.OPERATOR_PAUSED, TerminationReason.OPERATOR_STOPPED,
                    TerminationReason.USER_INTERRUPTED, TerminationReason.BUDGET_EXHAUSTED,
                    TerminationReason.RESOURCE_WAIT,
                }):
            await self.propose(runtime)

    async def finish(self, events: list[InstanceEvent], *, covers_cohort: bool = True) -> None:
        suspended = any(event.kind != "completed" or event.runtime.pending_approval_id
                        or event.runtime.termination_reason in {
                            TerminationReason.OPERATOR_PAUSED,
                            TerminationReason.OPERATOR_STOPPED,
                            TerminationReason.USER_INTERRUPTED,
                            TerminationReason.RESOURCE_WAIT,
                        } for event in events)
        await self.state("quiescent" if covers_cohort and not suspended else "held")


async def run_specialists(
    base: RuntimeState, deps: RuntimeDeps, *, settings: OrchestrationSettings,
) -> RuntimeState:
    """All runners/checkpoints stay owned by the existing InstanceDriver."""
    from redteam.app.bootstrap import settle_engagement

    deps = replace(deps, settings=settings)
    loop = SpecialistLifecycle(base, deps)
    policy = deps.specialist_policy
    assert policy is not None
    results: list[InstanceEvent] = []
    async with InstanceDriver(deps, settings=settings) as driver:
        await driver.own_run(str(base.run_id))
        await loop.state("active")
        await loop.propose(base)
        async for event in driver.drive_ready_tasks(
            base, max_instances=policy.max_instances, max_activations=policy.max_activations,
        ):
            results.append(event)
            # Other actors keep running in this SAME driver during judgments.
            await loop.returned(event)

        await loop.finish(results)
        held = next((event.runtime for event in results if event.runtime.pending_approval_id), None)
        if held is None:
            held = next((
                event.runtime for event in results
                if event.runtime.termination_reason == TerminationReason.RESOURCE_WAIT
            ), None)
        final = held or (results[-1].runtime if results else base.model_copy(update={
            "phase": AgentPhase.FINISHED,
            "termination_reason": TerminationReason.INVESTIGATION_HELD,
        }))
        # A returned RuntimeState remains an INSTANCE checkpoint projection.
        # Cohort totals belong in reports/shared accounting, never the resume token.
        if loop.diagnostic is not None:
            final = final.model_copy(update={"planning_diagnostic": loop.diagnostic})
        return await settle_engagement(final, deps)
