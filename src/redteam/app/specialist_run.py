"""Specialist judgments composed with fresh and reconciled native instances."""

from dataclasses import replace
from datetime import datetime, timezone
from typing import Literal

from redteam.core.contracts.events import EventType
from redteam.core.enums import AgentPhase
from redteam.orchestration.cognition import DecisionFailure
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.coordination import ChangeSet, CoordinationError, StateConflict
from redteam.orchestration.diagnostics import DecisionDiagnostic, error_type_for
from redteam.orchestration.evidence_validity import refresh_validity, validity_schedule
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
        self._seen_validity: str | None = None

    async def state(self, state: Literal["active", "held", "quiescent"]) -> None:
        async def set_state(tx: ChangeSet) -> None:
            run_id = str(self.base.run_id)
            outstanding = (TaskActivation.has_pending(tx, run_id) or pending_count(tx, run_id))
            tx.records[("specialist_lifecycle", run_id)] = {
                "state": "held" if state == "quiescent" and outstanding else state,
            }
        await self.planner.coordinator.change(set_state)

    async def propose(self, runtime: RuntimeState) -> None:
        before = None
        try:
            before = await validity_schedule(self.deps, runtime)
            if before is None:
                return
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
        # Only consume the version observed before this judgment. A validity
        # change during the model call must wake a fresh relevant snapshot.
        if before is not None:
            self._seen_validity = before.digest

    async def evidence_wakeup(self) -> datetime | None:
        """One code-grounded expiry/change check inside the native driver.

        Unchanged notifications cost no model request. A conflict/read failure
        holds this wakeup, without an instant timer retry or clearing fences.
        Planner dedup/CAS and the shared Run ledger still govern followups.
        """
        try:
            current = await validity_schedule(self.deps, self.base)
            if current is None:
                return None
            previous = current.digest
            if (current.next_expiry is not None
                    and current.next_expiry <= datetime.now(timezone.utc)):
                await refresh_validity(self.deps, self.base)
                current = await validity_schedule(self.deps, self.base)
                if current is None:
                    return None
            if current.digest != previous:
                await self.propose(self.base)
            elif self._seen_validity is None:
                self._seen_validity = current.digest
            elif current.digest != self._seen_validity:
                await self.propose(self.base)
            # Re-read after judgments. Clock expiry at the final CAS remains
            # authoritative; a past deadline is never returned as a spin loop.
            current = await validity_schedule(self.deps, self.base)
            if (current is not None and current.next_expiry is not None
                    and current.next_expiry <= datetime.now(timezone.utc)):
                # Another observed deadline may have elapsed during the model
                # call. Refresh it; the committed event wakes the next pass.
                await refresh_validity(self.deps, self.base)
                current = await validity_schedule(self.deps, self.base)
                if (current is not None and current.next_expiry is not None
                        and current.next_expiry <= datetime.now(timezone.utc)):
                    raise StateConflict("validity_wakeup_unresolved_expiry")
            return current.next_expiry if current is not None else None
        except Exception as exc:
            # CancelledError is BaseException and propagates through driver
            # teardown. No error text or sensitive evidence is logged here.
            self.diagnostic = DecisionDiagnostic(
                code="evidence_validity_wakeup_held", stage="evidence_validity",
                kind=("stale" if isinstance(exc, (CoordinationError, StateConflict))
                      else "storage" if isinstance(exc, OSError) else "validation"),
                summary="Evidence validity wakeup could not establish a current snapshot.",
                retryable=False, error_type=error_type_for(exc),
            )
        return None

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
            on_wakeup=loop.evidence_wakeup,
            wake_events=(EventType.EVIDENCE_VALIDITY_CHANGED,),
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
