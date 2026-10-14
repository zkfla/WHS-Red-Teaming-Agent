"""Default central planner/worker execution over one canonical state store."""

import asyncio
import logging
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.events import EventType
from redteam.core.contracts.mission import Provenance
from redteam.core.enums import AgentPhase, ApprovalStatus, MissionStatus
from redteam.core.ids import EngagementId, MissionId, RunId, new_id
from redteam.core.ports.control_plane import DurableControlRecovery
from redteam.orchestration.central_runtime import CentralEventBus, process_controls
from redteam.orchestration.cognition import DecisionFailure, ReasoningHalted
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.coordination import StateConflict
from redteam.orchestration.evidence_validity import (
    ExpiryWatch,
    ValidityChange,
    emit_allowed,
    next_expiry,
    queue_validity_change,
    refresh_validity,
    validity_change_envelope,
)
from redteam.orchestration.instance_driver import InstanceDriver
from redteam.orchestration.seed import seed_from_engagement
from redteam.orchestration.specialist_planner import SpecialistPlanner, specialist_planning_inputs
from redteam.orchestration.state import RuntimeState, TerminationReason
from redteam.orchestration.wakeup import EventWakeup

logger = logging.getLogger(__name__)


def _delivery_bus(deps: RuntimeDeps):
    """Raw bus for durable outbox delivery."""
    if isinstance(deps.event_bus, CentralEventBus):
        return deps.event_bus.bus
    return deps.event_bus


async def recheck_evidence_expiry(deps: RuntimeDeps, runtime: RuntimeState, watch: ExpiryWatch,
                                   *, baseline_silent: bool = False,
                                   ) -> tuple[list[ValidityChange], datetime | None]:
    """One bounded originals re-read; queue grounded changes for the planner.

    Reads evidence/graph originals only: never a model or tool call, never
    scope/approval/target authorization, and never deletion of
    originals/Findings/task results. With ``baseline_silent`` the observation
    only establishes the watch baseline (restart recovery reads original
    conditions without notifying). Otherwise real transitions are queued on
    the existing outbox as EvidenceValidityChanged with related evidence
    refs and a change reason, advancing the existing event sequence so the
    planner re-judges through its normal bounded path. While the mission is
    not ACTIVE the observation is absorbed and nothing is queued, so
    pause/stop/shutdown start no new work; resume and the authoritative
    final validity gate re-read the same originals.
    """
    projections = await refresh_validity(deps, runtime)
    now = datetime.now(timezone.utc)
    if baseline_silent:
        watch.observe(projections, now=now)
        return [], next_expiry(projections, now=now)
    # Peek first: the baseline advances only after the change is durably
    # queued, so a failed queue retries instead of dropping the change.
    changes = watch.pending_changes(projections, now=now)
    upcoming = next_expiry(projections, now=now)
    if not changes:
        watch.advance(projections)
        return [], upcoming
    if runtime.mission_id is None:
        raise StateConflict("evidence_expiry_mission_unavailable")
    mission = await deps.central_state.get_mission(runtime.mission_id)
    if mission is None or mission.status is None:
        raise StateConflict("evidence_expiry_mission_unavailable")
    if not emit_allowed(mission.status):
        watch.advance(projections)
        return [], upcoming
    envelopes = [
        validity_change_envelope(
            run_id=runtime.run_id, engagement_id=runtime.engagement_id, change=change,
        )
        for change in changes
    ]

    def queue_all(tx):
        for envelope in envelopes:
            queue_validity_change(tx, envelope)

    await deps.central_state.transaction(queue_all)
    watch.advance(projections)
    try:
        await deps.central_state.deliver_outbox(_delivery_bus(deps))
    except Exception as exc:  # noqa: BLE001 - durable row redelivers on later ticks
        logger.warning("evidence expiry delivery deferred: %s", type(exc).__name__)
    return changes, upcoming


@dataclass
class CentralRun:
    deps: RuntimeDeps
    runtime: RuntimeState
    driver: InstanceDriver
    instance_results: dict = field(default_factory=dict)
    development_double: bool = False

    async def recover(self, instance_id, *, transfer_owner=False):
        """Explicit original-actor collection/handoff, before resume."""
        reference = self.runtime.model_copy(update={"instance_id": instance_id})
        runner = self.driver.runner_for(reference)
        original = await runner.original_central_runtime(self.runtime.run_id, instance_id)
        return await self.driver.recover_central_instance(original, transfer_owner=transfer_owner)

    async def remaining_budget(self):
        remaining = await self.deps.central_state.transaction(
            lambda tx: tx.policy_budget_remaining(),
            write=False,
        )
        budget = self.deps.engagement.execution_budget
        return (
            remaining
            if remaining is not None
            else min(
                budget.max_actions,
                budget.max_tool_calls or budget.max_actions,
            )
        )

    async def status(self):
        state = self.deps.central_state
        return {
            "run_id": state.run_id,
            "development_double": self.development_double,
            "mission": (await state.get_mission(self.runtime.mission_id)).model_dump(mode="json"),
            "tasks": [task.model_dump(mode="json") for task in await state.list_tasks()],
            "executions": [item.model_dump(mode="json") for item in await state.list_executions()],
            "usage": await state.request_ledger().snapshot(state.run_id),
            "remaining_budget": await self.remaining_budget(),
            "instances": {
                identity: result.model_dump(mode="json")
                for identity, result in self.instance_results.items()
            },
        }

    async def run(self, *, settle=None):
        """A separate bounded planner alongside the existing as-ready actors."""
        await process_controls(self.deps)
        current = await self.deps.central_state.get_mission(self.runtime.mission_id)
        if current.status in {
            MissionStatus.COMPLETED, MissionStatus.CANCELLED, MissionStatus.PAUSED,
        }:
            return await self.settle(self.runtime)
        planner, wake = SpecialistPlanner(self.deps), asyncio.Event()
        kinds = planner.wake_events()
        updates = EventWakeup(
            run_id=self.runtime.run_id, engagement_id=self.runtime.engagement_id, event_types=kinds
        )
        await updates.attach(self.deps.event_bus)
        planning_halt = None
        planning_diagnostic = None
        planning_task = None
        control_task = None
        listening = True
        interval = self.deps.settings.resource_wait_timeout_seconds
        next_scan = datetime.now(timezone.utc) + timedelta(seconds=interval)
        last_planned = await self.deps.central_state.transaction(
            lambda tx: tx.latest_event_sequence(tuple(kinds)),
            write=False,
        )
        validity_watch = ExpiryWatch()
        validity_next: datetime | None = None
        validity_baselined = False
        validity_flush_due = False
        try:
            # Restart recovery reads original conditions without notifying;
            # queued-but-undelivered rows are redelivered durably below.
            _, validity_next = await recheck_evidence_expiry(
                self.deps, self.runtime, validity_watch, baseline_silent=True,
            )
            validity_baselined = True
            await self.deps.central_state.deliver_outbox(_delivery_bus(self.deps))
        except Exception as exc:  # noqa: BLE001 - expiry recheck retries on tick
            logger.warning("evidence expiry baseline skipped: %s", type(exc).__name__)

        async def changed(envelope):
            if listening and str(envelope.run_id) == str(self.runtime.run_id):
                wake.set()

        async def rethink():
            nonlocal planning_halt, planning_diagnostic
            if planning_halt is not None:
                return None
            await process_controls(self.deps)
            mission = await self.deps.central_state.get_mission(self.runtime.mission_id)
            if mission.status in {MissionStatus.PAUSED, MissionStatus.CANCELLED}:
                planning_halt = (
                    TerminationReason.OPERATOR_PAUSED
                    if mission.status is MissionStatus.PAUSED
                    else TerminationReason.OPERATOR_STOPPED
                )
                return None
            try:
                await planner.advance(
                    self.runtime.model_copy(
                        update={
                            "remaining_budget": await self.remaining_budget(),
                        },
                        deep=True,
                    )
                )
            except (DecisionFailure, ReasoningHalted) as exc:
                planning_diagnostic = exc.diagnostic
                mission = await self.deps.central_state.get_mission(self.runtime.mission_id)
                planning_halt = (
                    TerminationReason.OPERATOR_PAUSED
                    if mission.status is MissionStatus.PAUSED
                    else TerminationReason.OPERATOR_STOPPED
                    if mission.status is MissionStatus.CANCELLED
                    else exc.diagnostic.termination_reason or TerminationReason.PLANNING_ERROR
                )
            finally:
                wake.set()  # A planner response can finish without creating a Task.
            return None

        async def apply_controls():
            nonlocal planning_halt
            await process_controls(self.deps)
            mission = await self.deps.central_state.get_mission(self.runtime.mission_id)
            if mission.status in {MissionStatus.PAUSED, MissionStatus.CANCELLED}:
                planning_halt = (
                    TerminationReason.OPERATOR_PAUSED
                    if mission.status is MissionStatus.PAUSED
                    else TerminationReason.OPERATOR_STOPPED
                )
                self.driver.stop_active()
                if planning_task is not None and not planning_task.done():
                    planning_task.cancel()

        async def tick():
            nonlocal planning_task, next_scan, planning_halt, last_planned
            nonlocal validity_watch, validity_next, validity_baselined, validity_flush_due
            if control_task is not None and control_task.done():
                await control_task
            await apply_controls()
            if planning_task is not None and planning_task.done():
                if not (planning_task.cancelled() and planning_halt is not None):
                    await planning_task
                planning_task = None
            now = datetime.now(timezone.utc)
            periodic = now >= next_scan
            if periodic:
                next_scan = now + timedelta(seconds=interval)
            validity_changed = False
            if (not validity_baselined
                    or validity_next is None
                    or now >= validity_next
                    or updates.pending
                    or periodic):
                # New bindings arrive without a dedicated wake event, and a new
                # earlier deadline supersedes a far-future one: re-read current
                # originals on any activity or bounded scan and recompute the
                # earliest observed deadline.
                try:
                    queued, validity_next = await recheck_evidence_expiry(
                        self.deps, self.runtime, validity_watch,
                        baseline_silent=not validity_baselined,
                    )
                    validity_baselined = True
                    validity_changed = bool(queued)
                except Exception as exc:  # noqa: BLE001 - retry bounded, never raw text
                    logger.warning("evidence expiry recheck skipped: %s", type(exc).__name__)
                    validity_next = now + timedelta(seconds=interval)
            if validity_changed:
                # A queued change may still sit undelivered; redeliver on
                # later ticks until the dispatcher reports nothing pending.
                validity_flush_due = True
            if validity_flush_due:
                try:
                    remaining = await self.deps.central_state.deliver_outbox(
                        _delivery_bus(self.deps))
                    validity_flush_due = remaining > 0
                except Exception as exc:  # noqa: BLE001 - redelivery retries on tick
                    logger.warning("evidence expiry redelivery skipped: %s",
                                   type(exc).__name__)
            if (
                planning_task is None
                and planning_halt is None
                and (updates.pending or periodic or validity_changed)
            ):
                if updates.pending:
                    await updates.wait(timeout_seconds=interval)
                committed = await self.deps.central_state.transaction(
                    lambda tx: tx.latest_event_sequence(tuple(kinds)),
                    write=False,
                )
                if committed > last_planned:
                    last_planned = committed
                    planning_task = asyncio.create_task(
                        rethink(),
                        name="central:planner",
                    )
                    self.driver.track_background(planning_task)
            if planning_task is not None or await self.deps.central_state.transaction(
                lambda tx: tx.active_activation_count(),
                write=False,
            ):
                if validity_next is not None:
                    return min(next_scan, validity_next)
                return next_scan
            return None

        async def monitor_controls():
            # Also runs while the first planner request/provider is blocked.
            # SQL admission fences immediately; bounded scans deliver the stop.
            while listening:
                await asyncio.sleep(interval)
                await apply_controls()
                wake.set()

        for kind in kinds:
            await self.deps.event_bus.subscribe(kind, changed)
        try:
            # First planning completes before reservation; subsequent planning
            # runs independently of tool calls and first-completed delivery.
            control_task = asyncio.create_task(monitor_controls(), name="central:controls")
            planning_task = asyncio.create_task(rethink(), name="central:planner")
            self.driver.track_background(planning_task)
            try:
                await asyncio.wait((planning_task, control_task),
                                   return_when=asyncio.FIRST_COMPLETED)
                if control_task.done():
                    await control_task
                await planning_task
            except asyncio.CancelledError:
                if planning_halt is None:
                    raise
            planning_task = None
            async for result in self.driver.drive_ready_tasks(
                self.runtime,
                max_instances=self.deps.specialist_policy.max_instances,
                on_wakeup=tick,
                wake_events=tuple(kinds),
                wake=wake,
            ):
                self.instance_results[str(result.instance_id)] = result
                # A failed/held actor never shuts down independent siblings.
        finally:
            listening = False
            if control_task is not None:
                control_task.cancel()
                control_results = await asyncio.gather(control_task, return_exceptions=True)
                control_error = control_results[0]
                if isinstance(control_error, Exception):
                    raise control_error
            if planning_task is not None:
                if not planning_task.done():
                    planning_task.cancel()
                _, pending = await asyncio.wait(
                    (planning_task,), timeout=self.deps.settings.drain_timeout_seconds,
                )
                if pending:
                    raise RuntimeError("central_planner_drain_incomplete_run_ownership_retained")
                await asyncio.gather(planning_task, return_exceptions=True)
        usage = await self.deps.central_state.request_ledger().snapshot(str(self.runtime.run_id))
        actions = sum(item.runtime.actions_executed for item in self.instance_results.values())
        reason = planning_halt or TerminationReason.INVESTIGATION_HELD
        remaining = await self.remaining_budget()
        request_cap = self.deps.settings.llm_request_policy.max_requests
        if planning_halt is None and (
            remaining == 0 or (request_cap is not None and usage["spent"] >= request_cap)
        ):
            reason = TerminationReason.BUDGET_EXHAUSTED
        final = self.runtime.model_copy(
            update={
                "phase": AgentPhase.FAILED
                if reason == TerminationReason.PLANNING_ERROR
                else AgentPhase.FINISHED,
                "termination_reason": reason,
                "planning_diagnostic": planning_diagnostic,
                "actions_executed": actions,
                "remaining_budget": remaining,
                "model_requests_used": usage["spent"],
                "model_tokens_used": usage["known_tokens"],
                "model_usage_unknown": usage["unknown_usage"],
            }
        )
        if settle is True or (settle is None and not self.development_double):
            final = await self.settle(final)
        return final

    async def settle(self, final):
        """Close admission, bounded actors already returned, then assess current originals."""
        import json

        from redteam.orchestration.central_completion import complete_goal, goal_snapshot, unsafe
        from redteam.orchestration.coordination import CoordinationError, StateConflict
        from redteam.orchestration.reasoning_time import reasoning_time_failure

        state = self.deps.central_state
        await process_controls(self.deps)
        mission = await state.get_mission(self.runtime.mission_id)
        reason = final.termination_reason
        if mission.status is MissionStatus.COMPLETED:
            reason = TerminationReason.GOAL_ACHIEVED
        elif mission.status is MissionStatus.PAUSED:
            reason = TerminationReason.OPERATOR_PAUSED
        elif mission.status is MissionStatus.CANCELLED:
            reason = TerminationReason.OPERATOR_STOPPED
        elif reason not in {TerminationReason.OPERATOR_STOPPED, TerminationReason.OPERATOR_PAUSED,
                            TerminationReason.PLANNING_ERROR, TerminationReason.BUDGET_EXHAUSTED}:
            failure = reasoning_time_failure(self.deps.engagement, self.runtime)
            if failure:
                reason = (TerminationReason.BUDGET_EXHAUSTED if failure[1] else
                          TerminationReason.INVESTIGATION_HELD)
            else:
                await state.transaction(lambda tx: tx.set_settlement_gate(True))
                try:
                    await self.driver.quiesce()
                    errors = await complete_goal(self.deps, self.runtime.model_copy(deep=True))
                    if not errors:
                        reason = TerminationReason.GOAL_ACHIEVED
                    else:
                        def judgment(tx):
                            row = tx.db.execute(
                                "SELECT payload FROM state_requests WHERE run_id=? "
                                "AND kind='planner_termination' AND identity=?",
                                (tx.run_id, tx.run_id),
                            ).fetchone()
                            proposal = json.loads(row[0]) if row else None
                            fresh_candidates = (
                                proposal is None or "candidate_inputs" not in proposal
                                or proposal["candidate_inputs"] == tx.hypothesis_candidate_inputs()
                            )
                            return (proposal, goal_snapshot(tx, include_assessment=False),
                                    fresh_candidates)
                        proposal, (_, tasks, stamp), fresh_candidates = await state.transaction(
                            judgment, write=False,
                        )
                        history = await state.list_executions()
                        if (proposal and proposal["termination"] == "exhausted"
                                and proposal["inputs"] == stamp
                                and fresh_candidates
                                and not any(unsafe(item) for item in history)
                                and not any(task.status.value in {"ready", "pending", "running"}
                                            for task in tasks)):
                            reason = TerminationReason.EXHAUSTED
                        else:
                            reason = TerminationReason.INVESTIGATION_HELD
                except (DecisionFailure, ReasoningHalted) as exc:
                    reason = exc.diagnostic.termination_reason or TerminationReason.PLANNING_ERROR
                    final = final.model_copy(update={"planning_diagnostic": exc.diagnostic})
                except (CoordinationError, StateConflict):
                    reason = TerminationReason.INVESTIGATION_HELD
                finally:
                    if reason != TerminationReason.GOAL_ACHIEVED:
                        await state.transaction(lambda tx: tx.set_settlement_gate(False))
        mission = await state.get_mission(self.runtime.mission_id)
        if mission.status is MissionStatus.COMPLETED:
            reason = TerminationReason.GOAL_ACHIEVED
        elif mission.status is MissionStatus.CANCELLED:
            reason = TerminationReason.OPERATOR_STOPPED
        elif mission.status is MissionStatus.PAUSED:
            reason = TerminationReason.OPERATOR_PAUSED
        if mission.status not in {MissionStatus.COMPLETED, MissionStatus.CANCELLED}:
            def save(tx):
                current = tx.get_mission(tx.binding()[0])
                if current.status not in {MissionStatus.COMPLETED, MissionStatus.CANCELLED}:
                    tx.update_mission(current.model_copy(update={
                        "revision": current.revision + 1, "termination_reason": reason,
                        "updated_at": datetime.now(timezone.utc),
                    }), expected_revision=current.revision)
            await state.transaction(save)
            mission = await state.get_mission(self.runtime.mission_id)
        usage = await state.request_ledger().snapshot(str(self.runtime.run_id))
        history = await state.list_executions()
        pending_approval = None
        if (mission.status not in {
            MissionStatus.COMPLETED, MissionStatus.CANCELLED, MissionStatus.FAILED,
        } and isinstance(self.deps.control_plane, DurableControlRecovery)):
            for item in history:
                if item.wait_reason and item.approval_ref:
                    approval = await self.deps.control_plane.inspect_approval(item.approval_ref)
                    if approval is not None and approval.status is ApprovalStatus.PENDING:
                        pending_approval = item.approval_ref
                        break
        final = final.model_copy(update={
            "termination_reason": reason, "mission_status": mission.status,
            "phase": AgentPhase.FAILED if reason == TerminationReason.PLANNING_ERROR
                     else AgentPhase.FINISHED,
            "actions_executed": sum(item.result_evidence_id is not None for item in history),
            "model_requests_used": usage["spent"], "model_tokens_used": usage["known_tokens"],
            "model_usage_unknown": usage["unknown_usage"],
            "pending_approval_id": pending_approval,
        })
        await state.transaction(lambda tx: tx.save_planning_diagnostics(
            final.planning_diagnostic, final.planning_diagnostic_history,
            termination_reason=reason,
        ))
        self.runtime = self.runtime.model_copy(update={"mission_status": mission.status})
        return final

    async def resume(self, *, instance_id=None, transfer_owner=True):
        """Original actor recovery through the same bounded InstanceDriver."""
        from redteam.orchestration.central_completion import unsafe
        from redteam.orchestration.central_recovery import recovery_diagnostic
        if (self.runtime.planning_diagnostic is not None
                and self.runtime.planning_diagnostic.stage == "execution_reconciliation"):
            self.runtime = self.runtime.model_copy(update={"planning_diagnostic": None})
        state = self.deps.central_state
        if instance_id is not None:
            terminal = await terminal_selected_runtime(state, self.runtime, instance_id)
            if terminal is not None:
                return terminal
        selected = {str(instance_id)} if instance_id is not None else None
        inventory = await state.activation_inventory(instance_ids=selected)
        if selected is not None:
            inventory = [row for row in inventory if row["instance_id"] in selected]
            if not inventory:
                raise ValueError("central_original_instance_missing")
        await state.transaction(lambda tx: tx.set_settlement_gate(False))
        checkpoint_instances = set()
        originals = {}
        from redteam.orchestration.coordination import CoordinationError
        # Prove every original before starting any actor. Only a proved
        # zero-entry reservation can be recovered without an SDK snapshot.
        for row in inventory:
            identity = row["instance_id"]
            task = await state.get_task(row["task_id"])
            if task.status.value in {"completed", "cancelled", "failed"}:
                continue
            try:
                originals[identity] = await self.driver.runner_for(
                    self.runtime.model_copy(update={"instance_id": identity})
                ).original_central_runtime(self.runtime.run_id, identity)
                checkpoint_instances.add(identity)
            except CoordinationError:
                if row["launch_stage"] != "reserved":
                    raise
        unstarted = await self.driver.recover_unstarted_activations(
            str(self.runtime.run_id), checkpoint_instances=checkpoint_instances,
            instance_ids=selected,
        )
        items = []
        recovery_errors = False
        for row in inventory:
            identity = row["instance_id"]
            if instance_id is not None and str(instance_id) != identity:
                continue
            task = await state.get_task(row["task_id"])
            if task.status.value in {"completed", "cancelled", "failed"}:
                continue
            if identity not in originals:
                continue
            original = originals[identity]
            recovered, outcome = await self.driver.recover_central_instance(
                original, transfer_owner=transfer_owner,
            )
            if outcome.ready:
                items.append((recovered, None))
            elif outcome.errors:
                # Preserve the structured cause on the root result/report too.
                recovery_errors = True
                self.runtime = self.runtime.model_copy(update={
                    "planning_diagnostic": recovery_diagnostic(outcome.errors),
                })
        if instance_id is not None and not any(row["instance_id"] == str(instance_id)
                                               for row in inventory):
            raise ValueError("central_original_instance_missing")
        limit = self.deps.specialist_policy.max_instances
        unstarted = [item for item in unstarted if instance_id is None
                     or str(item.instance_id) == str(instance_id)]
        actors = [(item, None, True) for item in unstarted] + [
            (runtime, reconciliation, False) for runtime, reconciliation in items
        ]
        for start in range(0, len(actors), limit):
            batch = actors[start:start + limit]
            async for outcome in self.driver.drive_ready_tasks(
                self.runtime, max_instances=limit, allow_new_tasks=False,
                resume_items=[(rt, rec) for rt, rec, fresh in batch if not fresh],
                unstarted_items=[rt for rt, _, fresh in batch if fresh],
                selected_instances=selected,
            ):
                if outcome.runtime is not None:
                    self.instance_results[str(outcome.instance_id)] = outcome
        if selected is not None:
            # Partial recovery has no authority to judge/close the whole goal.
            row, = inventory
            outcome = self.instance_results.get(row["instance_id"])
            task = await state.get_task(row["task_id"])
            final = outcome.runtime if outcome is not None else originals.get(
                row["instance_id"], self.runtime.model_copy(update={
                    "instance_id": row["instance_id"], "active_task_id": task.task_id,
                    "current_agent": task.assigned_agent,
                }),
            )
            mission = await state.get_mission(self.runtime.mission_id)
            return final.model_copy(update={
                "phase": AgentPhase.FAILED if outcome and outcome.kind == "failed"
                         else AgentPhase.FINISHED,
                "termination_reason": TerminationReason.INVESTIGATION_HELD,
                "mission_status": mission.status,
                "planning_diagnostic": self.runtime.planning_diagnostic,
            })
        if recovery_errors or any(unsafe(item) for item in await state.list_executions()):
            return await self.settle(self.runtime.model_copy(update={
                "phase": AgentPhase.FINISHED,
                "termination_reason": TerminationReason.INVESTIGATION_HELD,
            }))
        return await self.run()


@asynccontextmanager
async def open_central_run(config, *, directory, settings, run_id=None,
                           engagement_id=None, resume=False, **components):
    """Explicit API selection using the existing application/provider assembly.

    New selections require a separate directory. Resume requires both original
    IDs and existing state/checkpoints; it never creates replacements. The CLI
    and application use central state by default. Catalog discovery may open
    configured transports; no planner/model completion or target tool runs here.
    """
    from redteam.app.bootstrap import build_application, build_central_storage
    from redteam.orchestration.coordination import content_hash

    root = Path(directory).resolve()
    state_path = root / "state.sqlite3"
    if resume:
        if (run_id is None or engagement_id is None or not state_path.is_file()
                or not (root / "checkpoints.sqlite3").is_file()):
            raise ValueError("central_resume_requires_original_ids_state_and_checkpoint")
    elif root.exists() and any(root.iterdir()):
        raise ValueError("central_new_selection_requires_separate_empty_directory")
    if any(key in components for key in (
        "central_state", "engagement_id", "orchestration_settings", "mcp_stack",
        "evidence_dir", "audit_dir", "approval_dir",
    )):
        raise ValueError("central_selection_storage_identity_is_owned_by_session")
    run_id = RunId(str(run_id or new_id("run")))
    engagement_id = EngagementId(str(engagement_id or new_id("eng")))
    selected = settings.model_copy(update={
        "checkpointing": True, "checkpoint_backend": "sqlite",
        "checkpoint_path": str(root / "checkpoints.sqlite3"),
    })
    engagement = config.engagement.to_engagement(engagement_id=engagement_id)
    state, _ = await build_central_storage(engagement, run_id=str(run_id), path=state_path)
    async with AsyncExitStack() as stack:
        engagement, deps = await build_application(
            config, central_state=state, engagement_id=engagement_id,
            orchestration_settings=selected, mcp_stack=stack,
            evidence_dir=root / "evidence", audit_dir=root / "audit",
            approval_dir=root / "approvals", **components,
        )
        driver = await stack.enter_async_context(InstanceDriver(deps))
        await driver.own_run(str(run_id))
        if resume:
            mission_id, _ = await state.transaction(lambda tx: tx.binding(), write=False)
            mission = await state.get_mission(mission_id)
            if mission is None:
                raise ValueError("central_original_mission_missing")
        else:
            now = datetime.now(timezone.utc)
            from redteam.orchestration.engagement_planner import plan_mission
            mission = plan_mission(engagement, MissionId(new_id("mission")), now=now)
            await state.create_mission(mission)
        await state.bind_run(
            str(mission.mission_id), scope=engagement.scope,
            binding_hash=content_hash(specialist_planning_inputs(deps)),
        )
        await state.request_ledger().bind(str(run_id), selected.llm_request_policy)
        await bind_run_metadata(deps)
        if resume:
            await state.request_ledger().recover_run(str(run_id))
            await deps.control_plane.rehydrate_run(
                engagement_id, run_id, started_at=mission.created_at,
            )
        else:
            await seed_from_engagement(AttackGraphService(deps.graph_store), engagement,
                                       run_id=run_id, execution_recipes=deps.execution_recipes)
        runtime = RuntimeState(
            run_id=run_id, engagement_id=engagement_id, mission_id=mission.mission_id,
            current_goal=mission.objective, started_at=mission.created_at,
            remaining_budget=engagement.execution_budget.max_actions,
            component_binding=deps.component_binding,
            llm_request_policy=selected.llm_request_policy.model_dump(mode="json"),
        )
        yield CentralRun(deps, runtime, driver)


async def bind_run_metadata(deps, *, before_bind=None):
    """Original storage references and actual applied limits, not a second Runtime."""
    import json

    from redteam.orchestration.central_runtime import tool_limit, worker_limit
    from redteam.orchestration.coordination import StateConflict

    metadata = {
        "storage_refs": deps.central_state.original_store_paths,
        "settings": deps.settings.central_payload(),
        "effective_limits": {
            "workers": worker_limit(deps), "tool_calls": tool_limit(deps),
            "per_target": deps.settings.per_target_concurrency,
            "llm_requests": deps.settings.llm_request_policy.model_dump(mode="json"),
            "execution_budget": deps.engagement.execution_budget.model_dump(mode="json"),
        },
    }
    binding = deps.component_binding
    if binding is not None and (
        binding.llm_connection_fingerprint is not None
        or binding.execution_environment_fingerprint is not None
        or binding.network_environment_fingerprint is not None
    ):
        # New optional provider identity remains readable before transports are
        # rebuilt. Absent identities keep historical central metadata unchanged.
        metadata["component_binding"] = binding.model_dump(mode="json")
    artifact_store = getattr(deps, "artifact_store", None)
    fixture_runner = getattr(deps, "code_fixture_runner", None)
    if artifact_store is not None:
        metadata["code_features"] = {
            "code_artifacts": "enabled",
            "code_fixtures": (
                "configured" if fixture_runner is not None else "unconfigured"
            ),
            "artifact_store_fingerprint": artifact_store.fingerprint,
            "fixture_runner_fingerprint": (
                fixture_runner.fingerprint if fixture_runner is not None else None
            ),
        }
    def bind(tx):
        if before_bind is not None:
            before_bind(tx)
        row = tx.db.execute("SELECT payload FROM state_requests WHERE run_id=? "
                            "AND kind='run_configuration' AND identity=?",
                            (tx.run_id, tx.run_id)).fetchone()
        if row is not None:
            original = json.loads(row[0])
            # Old central records contained inert legacy backend/ranking fields.
            # Ignore only those retired fields; original checkpoints, operative
            # limits and storage references must still match exactly.
            original["settings"] = type(deps.settings).model_validate(
                original["settings"],
            ).central_payload()
            if original != metadata:
                raise StateConflict("central_original_storage_or_limits_changed")
        tx.db.execute("INSERT OR IGNORE INTO state_requests VALUES(?,'run_configuration',?,?)",
                      (tx.run_id, tx.run_id, json.dumps(metadata)))
    await deps.central_state.transaction(bind)


async def central_root_runtime(deps):
    state = deps.central_state
    mission_id = await state.transaction(lambda tx: tx.binding()[0], write=False)
    mission = await state.get_mission(mission_id)
    return RuntimeState(
        run_id=RunId(state.run_id), engagement_id=EngagementId(state.engagement_id),
        mission_id=mission.mission_id, current_goal=mission.objective,
        started_at=mission.created_at, component_binding=deps.component_binding,
        remaining_budget=deps.engagement.execution_budget.max_actions,
        llm_request_policy=deps.settings.llm_request_policy.model_dump(mode="json"),
    )


async def run_central_engagement(
    engagement, *, deps, objective=None, run_id=None, instance_id=None, watch=None,
):
    from redteam.orchestration.central_runtime import event
    from redteam.orchestration.coordination import CoordinationError, content_hash
    from redteam.orchestration.engagement_planner import plan_mission
    if instance_id is not None or (run_id is not None and str(run_id) != deps.central_state.run_id):
        raise ValueError("central_run_identity_is_owned_by_selected_storage")
    state = deps.central_state
    async with InstanceDriver(deps) as driver:
        await driver.own_run(state.run_id)
        try:
            await state.transaction(lambda tx: tx.binding(), write=False)
        except CoordinationError:
            now = datetime.now(timezone.utc)
            mission = plan_mission(engagement, MissionId(new_id("mission")),
                                   objective=objective, now=now)
            await state.create_mission(mission)
            await state.bind_run(
                str(mission.mission_id), scope=engagement.scope,
                binding_hash=content_hash(specialist_planning_inputs(deps)),
            )
            await state.request_ledger().bind(state.run_id, deps.settings.llm_request_policy)
            await bind_run_metadata(deps)
            await seed_from_engagement(
                AttackGraphService(deps.graph_store), engagement,
                run_id=RunId(state.run_id), execution_recipes=deps.execution_recipes,
            )
        runtime = await central_root_runtime(deps)
        await deps.event_bus.publish(event(state, EventType.RUN_STARTED, {
            "mission_id": str(runtime.mission_id), "effective_limits": {
                "workers": deps.specialist_policy.max_instances,
            },
        }))
        await deps.event_bus.publish(event(state, EventType.MISSION_CREATED, {
            "mission_id": str(runtime.mission_id),
        }))
        session = CentralRun(deps, runtime, driver)
        final = await session.run()
        if watch is not None:
            final = await watch(session, final)
        await publish_central_report(final, deps)
        return final


async def publish_central_report(final, deps):
    from redteam.app.report_store import save_terminal_report
    from redteam.orchestration.central_runtime import event
    from redteam.orchestration.reporting import ReportingAgent
    tasks = await deps.central_state.list_tasks()
    required = {str(identity) for task in tasks if task.required
                for identity in task.related_hypothesis_ids}
    report = await ReportingAgent().project(
        final, graph=AttackGraphService(deps.graph_store), evidence_store=deps.evidence_store,
        work_store=None, task_store=deps.central_state, required_hypothesis_ids=required,
        agent_definitions=deps.agents.definitions(), central_state=deps.central_state,
    )
    import json
    configuration = await deps.central_state.transaction(lambda tx: json.loads(tx.db.execute(
        "SELECT payload FROM state_requests WHERE run_id=? AND kind='run_configuration' "
        "AND identity=?", (tx.run_id, tx.run_id),
    ).fetchone()[0]), write=False)
    report.effective_limits = configuration["effective_limits"]
    save_terminal_report(report)  # export only; CLI reads current central originals
    await deps.event_bus.publish(event(deps.central_state, "ReportProjected", {
        "ready_for_release": report.ready_for_release, "finding_count": report.finding_count,
    }))
    if final.termination_reason not in {
        TerminationReason.OPERATOR_PAUSED, TerminationReason.INVESTIGATION_HELD,
    }:
        await deps.event_bus.publish(event(deps.central_state, EventType.RUN_FINISHED, {
            "termination_reason": final.termination_reason, "mission_id": str(final.mission_id),
        }))
    return report


async def terminal_selected_runtime(state, runtime, instance_id):
    """Read a terminal selection without opening its SDK or judging its siblings."""
    from redteam.core.enums import TaskStatus
    from redteam.orchestration.coordination import StateConflict

    inventory = await state.activation_inventory(instance_ids={str(instance_id)})
    if not inventory:
        raise ValueError("central_original_instance_missing")
    row, = inventory
    task = await state.get_task(row["task_id"])
    if (task is None or task.occupant_owner != row["owner"]
            or task.ownership_version != row["ownership_version"]):
        raise StateConflict("activation_original_binding_mismatch")
    mission = await state.get_mission(runtime.mission_id)
    if task.status not in {TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED}:
        return None
    return runtime.model_copy(update={
        "instance_id": instance_id, "active_task_id": task.task_id,
        "current_agent": task.assigned_agent, "task_owner": task.occupant_owner,
        "task_ownership_version": task.ownership_version,
        "phase": AgentPhase.FAILED if task.status is TaskStatus.FAILED else AgentPhase.FINISHED,
        "termination_reason": mission.termination_reason or TerminationReason.INVESTIGATION_HELD,
        "mission_status": mission.status,
    })


async def resume_central_engagement(deps, *, instance_id=None, watch=None):
    from redteam.app.legacy_migration import migrated_runtime_guard
    async with migrated_runtime_guard(deps):
        return await _resume_central_engagement(deps, instance_id=instance_id, watch=watch)


async def _resume_central_engagement(deps, *, instance_id=None, watch=None):
    from redteam.core.contracts.mission import OperatorIntent
    from redteam.core.ids import OperatorIntentId
    from redteam.orchestration.coordination import content_hash
    from redteam.orchestration.operator_intents.models import IntentRecord
    state = deps.central_state
    if instance_id is not None:
        runtime = await central_root_runtime(deps)
        terminal = await terminal_selected_runtime(state, runtime, instance_id)
        if terminal is not None:
            await state.bind_run(
                str(runtime.mission_id), scope=deps.engagement.scope,
                binding_hash=content_hash(specialist_planning_inputs(deps)),
            )
            await bind_run_metadata(deps)
            await state.request_ledger().bind(state.run_id, deps.settings.llm_request_policy)
            await publish_central_report(terminal, deps)
            return terminal
    async with InstanceDriver(deps) as driver:
        await driver.own_run(state.run_id)
        runtime = await central_root_runtime(deps)
        mission = await state.get_mission(runtime.mission_id)
        await state.bind_run(
            str(runtime.mission_id), scope=deps.engagement.scope,
            binding_hash=content_hash(specialist_planning_inputs(deps)),
        )
        await bind_run_metadata(deps)
        await state.request_ledger().bind(state.run_id, deps.settings.llm_request_policy)
        if instance_id is None:
            await state.request_ledger().recover_run(state.run_id)
        await deps.control_plane.rehydrate_run(
            runtime.engagement_id, runtime.run_id, started_at=runtime.started_at,
        )
        if mission.status is MissionStatus.PAUSED:
            identity = OperatorIntentId(new_id("intent"))
            await state.append(IntentRecord(
                intent=OperatorIntent(
                    intent_id=identity, mission_id=runtime.mission_id,
                    intent_type="resume", instruction="Resume the original central Run",
                    created_at=datetime.now(timezone.utc),
                    provenance=Provenance(source="operator:cli"),
                ), run_id=runtime.run_id, correlation_id=str(identity),
            ))
            await process_controls(deps)
        if instance_id is None and mission.status in {
            MissionStatus.COMPLETED, MissionStatus.CANCELLED,
        }:
            final = await CentralRun(deps, runtime, driver).settle(runtime)
        else:
            session = CentralRun(deps, runtime, driver)
            final = await session.resume(instance_id=instance_id)
            if watch is not None:
                final = await watch(session, final)
        await publish_central_report(final, deps)
        return final


async def settle_central_engagement(runtime, deps):
    async with InstanceDriver(deps) as driver:
        await driver.own_run(str(runtime.run_id))
        final = await CentralRun(deps, await central_root_runtime(deps), driver).settle(runtime)
        await publish_central_report(final, deps)
        return final
