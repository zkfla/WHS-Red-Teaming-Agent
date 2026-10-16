"""EXECUTE — only reachable after an ALLOW ``PolicyDecision``.

Runs the tool the approved ``PlanStep`` declared, and owns execution
accounting (budget, consecutive failures).
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta, timezone

from redteam.attack_graph.identity import node_id_for_target
from redteam.core.contracts.events import EventType
from redteam.core.contracts.execution import ExecutionReference
from redteam.core.contracts.policy import ControlContext, PolicyDecision
from redteam.core.contracts.tool import (
    EXECUTION_STARTED_METADATA_KEY,
    ToolError,
    ToolInvocation,
    ToolResult,
)
from redteam.core.convert import build_tool_invocation
from redteam.core.enums import (
    AgentPhase,
    PolicyVerdict,
    Severity,
    ToolHealthStatus,
)
from redteam.core.ports.control_plane import InvocationAdmissionControl
from redteam.core.ports.tool_gateway import ToolStartAdmissionGateway, ToolStartRejected
from redteam.orchestration.action_naming import (
    canonical_action_key,
    tool_matches_action_namespace,
)
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.diagnostics import DecisionDiagnostic, operational_exception_diagnostic
from redteam.orchestration.engine import claim_action_admission, validate_current_action
from redteam.orchestration.execution_journal import append_execution_receipt
from redteam.orchestration.long_jobs import LongJobManager, LongJobState
from redteam.orchestration.nodes._support import advance, publisher_for
from redteam.orchestration.runtime_observation import (
    RuntimeObservation,
    append_runtime_observation,
)
from redteam.orchestration.state import GraphState
from redteam.orchestration.work.models import WorkStatus


class InvocationAdmissionDenied(ToolStartRejected):
    def __init__(self, decision: PolicyDecision):
        self.decision = decision
        super().__init__("current control admission denied")


def _native_lineage(step, runtime, work_id=None) -> dict:
    """Native call lineage for execution events.

    Read-only: the runtime owns ``execution_origin_version`` /
    ``model_tool_call_id`` / ``mission_task_id`` / ``task_purpose_hash``.
    Missing values stay at legacy defaults; no path is invented here and
    no validation exemption follows from origin -- admission still runs
    through ``validate_current_action`` / ``claim_action_admission``.
    """
    origin_version = getattr(step, "execution_origin_version", 0)
    if origin_version is None:
        origin_version = getattr(step, "origin_version", 0)
    try:
        origin_version = int(origin_version or 0)
    except (TypeError, ValueError):
        origin_version = 0
    mission_task_id = getattr(step, "mission_task_id", None)
    if mission_task_id is None:
        mission_task_id = getattr(runtime, "active_task_id", None)
    lineage: dict = {
        "execution_origin_version": origin_version,
        "model_tool_call_id": str(getattr(step, "model_tool_call_id", "") or ""),
        "task_purpose_hash": str(getattr(step, "task_purpose_hash", "") or ""),
        "candidate_key": str(getattr(step, "candidate_key", "") or ""),
    }
    if mission_task_id is not None:
        lineage["mission_task_id"] = str(mission_task_id)
    if work_id is not None:
        lineage["work_id"] = str(work_id)
    return lineage


async def _invocation_context(deps, runtime, proposal, work_id, tool_id=None):
    items = ([])
    others = sum(
        item.work_id != work_id and item.status in {WorkStatus.LEASED, WorkStatus.RUNNING}
        for item in items
    )
    from redteam.orchestration.central_state import execution_occupies_capacity
    others = sum(item.action_id != proposal.action_id and execution_occupies_capacity(item)
                 for item in await deps.central_state.list_executions())
    return ControlContext(
        engagement_id=proposal.engagement_id,
        agent_id=proposal.agent_id,
        correlation_id=str(proposal.action_id),
        in_flight_actions=others,
        selected_tool_id=tool_id,
        metadata={"run_id": str(proposal.run_id)},
    )


async def _renew_lease_until_stopped(
    *,
    deps: RuntimeDeps,
    work_id: str,
    owner: str,
    stop: asyncio.Event,
    lease_lost: asyncio.Event,
) -> None:
    """Keep a running tool's lease alive without extending it after return."""

    ttl_seconds = deps.settings.lease_ttl_seconds
    interval = max(min(ttl_seconds / 3, 10.0), 0.01)
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        # Python 3.13 keeps ``asyncio.TimeoutError`` distinct from the builtin
        # ``TimeoutError`` again, so catch the asyncio spelling explicitly.
        except asyncio.TimeoutError:
            pass
        try:
            assert deps.work_store is not None
            renewed = await deps.work_store.heartbeat(
                work_id,
                owner=owner,
                now=datetime.now(timezone.utc),
                ttl=timedelta(seconds=ttl_seconds),
            )
        except Exception:  # noqa: BLE001 - losing the ledger is losing the lease
            lease_lost.set()
            return
        if renewed is None:
            lease_lost.set()
            return


async def _invoke_with_semaphore(
    deps: RuntimeDeps,
    invocation: ToolInvocation,
    *,
    runtime=None,
    step=None,
    work_id=None,
    proposal=None,
    decision=None,
) -> ToolResult:
    """Invoke the tool, honoring the run-wide execution bound.

    The semaphore is the *actual* ``RateLimitPolicy.max_concurrent_actions``
    gate: policy admission may be serialized, but approved tool calls can
    otherwise run in parallel across fan-out workers. ``None`` leaves
    execution unbounded (legacy).
    """

    async def before_start():
        problems = await validate_current_action(deps, runtime, step, proposal)
        if problems:
            raise ToolStartRejected("action changed before invocation")
        await claim_action_admission(deps, runtime, step, proposal)
        from redteam.orchestration.central_runtime import start_execution

        # Re-read and enter under the SAME lock as proposal admission.
        # Otherwise two workers can both observe the last free slot.
        async with deps.policy_lock:
            context = await _invocation_context(
                deps, runtime, proposal, work_id, invocation.tool_id,
            )
            if isinstance(deps.control_plane, InvocationAdmissionControl):
                admitted = await deps.control_plane.admit_invocation(
                    proposal, decision, context, receipt_id=str(invocation.invocation_id),
                )
                if admitted.verdict is not PolicyVerdict.ALLOW:
                    raise InvocationAdmissionDenied(admitted)
            await start_execution(deps, runtime, invocation, step)
        return

    async def invoke():
        if deps.long_job_adapter is None:
            if isinstance(deps.tool_gateway, ToolStartAdmissionGateway):
                return await deps.tool_gateway.invoke_with_admission(invocation, before_start)
            await before_start()
            return await deps.tool_gateway.invoke(invocation)
        manager = LongJobManager(deps.long_job_adapter)
        work = (await deps.central_state.get_execution(str(invocation.invocation_id)))
        job_id = (work.job_ref) if work else None
        if job_id is None:
            await before_start()
            job_id = await manager.submit(invocation)
            current = await deps.central_state.get_execution(str(invocation.invocation_id))
            await deps.central_state.transaction(lambda tx: tx.update_execution(
                current.model_copy(update={"job_ref": job_id, "wait_reason": "job"}),
                owner=runtime.work_owner, ownership_version=runtime.task_ownership_version,
                now=datetime.now(timezone.utc).timestamp(),
            ))
        try:
            deadline = asyncio.get_running_loop().time() + invocation.timeout_seconds
            while True:
                status = await manager.status(job_id)
                if status.job_id != job_id:
                    raise RuntimeError("backend returned another job status")
                if status.state in {
                    LongJobState.SUCCEEDED,
                    LongJobState.FAILED,
                    LongJobState.CANCELLED,
                }:
                    result = (await manager.result(job_id)).require_result()
                    if (
                        result.action_id != invocation.action_id
                        or result.tool_id != invocation.tool_id
                    ):
                        raise RuntimeError("long job returned an unrelated result")
                    return result
                if status.state is LongJobState.UNKNOWN:
                    raise RuntimeError("long job execution state is unknown; do not resubmit")
                # Return the actor slot; the original job/resource/intent remains.
                now = datetime.now(timezone.utc)
                return ToolResult(
                    invocation_id=invocation.invocation_id, action_id=invocation.action_id,
                    tool_id=invocation.tool_id, success=False, started_at=now, finished_at=now,
                    metadata={EXECUTION_STARTED_METADATA_KEY: True,
                              "execution_uncertain": True, "job_id": job_id},
                    error=ToolError(
                        code="job_pending", message="Original job is still active.",
                    ),
                )
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError("registered job is still running; collect its result later")
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            # Keep the registered identity even if cancellation is unconfirmed.
            with suppress(Exception):
                await asyncio.wait_for(
                    manager.cancel(job_id), timeout=deps.settings.drain_timeout_seconds,
                )
            raise

    semaphore = deps.tool_semaphore
    if semaphore is None:
        return await invoke()
    async with semaphore:
        return await invoke()


async def execute_node(state: GraphState, *, deps: RuntimeDeps) -> GraphState:
    runtime = state["runtime"]
    proposal = state["action_proposal"]
    decision = state["policy_decision"]
    step = state.get("plan_step")
    assert proposal is not None and decision is not None and step is not None

    correlation_id = str(proposal.action_id)
    publisher = publisher_for(state, deps, "orchestration.execute")

    invocation = build_tool_invocation(
        proposal,
        decision,
        tool_id=step.tool_id,
        timeout_seconds=deps.settings.tool_timeout_seconds,
        correlation_id=correlation_id,
    )
    prepared = state.get("prepared_invocation")
    if (prepared is None or prepared.action_id != invocation.action_id
            or prepared.tool_id != invocation.tool_id
            or prepared.arguments != invocation.arguments
            or prepared.execution_context != invocation.execution_context):
        raise ValueError("central_prepared_invocation_changed")
    invocation = prepared

    started = None

    work_id = state.get("current_work_id")
    owner = runtime.work_owner
    lease_lost = asyncio.Event()
    heartbeat_task: asyncio.Task[None] | None = None
    lease_ready = True
    if step.binding_mode == "selected_tool":
        binding_ready = bool(
            proposal.binding_mode == step.binding_mode and proposal.tool_id == step.tool_id
        )
    else:
        try:
            canonical_action_key(proposal.capability, proposal.operation)
            binding_ready = bool(
                step.capability == proposal.capability
                and step.operation == proposal.operation
                and tool_matches_action_namespace(step.tool_id, proposal.capability)
            )
        except ValueError:
            binding_ready = False
    descriptor = None
    if binding_ready and deps.tool_catalog is not None:
        descriptor = deps.tool_catalog.get(step.tool_id)
        binding_ready = bool(
            descriptor is not None
            and (
                step.binding_mode == "selected_tool"
                or proposal.capability in descriptor.capability_tags
            )
            and descriptor.health_status is not ToolHealthStatus.UNHEALTHY
        )
    if step.delegation_id and runtime.delegations.get(step.delegation_id) is None:
        binding_ready = False
    if work_id is not None:
        try:
            assert deps.work_store is not None
            lease_ready = (
                await deps.work_store.heartbeat(
                    work_id,
                    owner=owner,
                    now=datetime.now(timezone.utc),
                    ttl=timedelta(seconds=deps.settings.lease_ttl_seconds),
                )
                is not None
            )
        except Exception:  # noqa: BLE001 - fail closed when lease state is unknown
            lease_ready = False

    admission_denial = None
    # Central execution makes exactly one provider attempt; a retryable
    # failure is returned to the model instead of re-run here.
    retry_attempts = 0
    orchestration_diagnostic = None
    any_provider_started = False
    invocation_entered = False
    reference: ExecutionReference | None = None
    try:
        path_problems = await validate_current_action(deps, runtime, step, proposal)
        if path_problems:
            binding_ready = False
        if not binding_ready:
            raise RuntimeError("tool binding is no longer valid for the approved action")
        if not lease_ready:
            raise RuntimeError("work lease was lost before tool execution")
        if deps.execution_recovery is not None:
            reference = deps.execution_recovery.prepare_reference(invocation)
        started = await publisher.publish(
            EventType.TOOL_EXECUTION_STARTED,
            {
                "invocation_id": invocation.invocation_id,
                "action_id": invocation.action_id,
                "tool_id": invocation.tool_id,
                "candidate_key": step.candidate_key,
                "hypothesis_id": proposal.hypothesis_id,
                **_native_lineage(step, runtime, state.get("current_work_id")),
            },
            correlation_id=correlation_id,
            causation_id=state.get("last_event_id"),
        )
        if work_id is not None:
            stop_heartbeat = asyncio.Event()
            heartbeat_task = asyncio.create_task(
                _renew_lease_until_stopped(
                    deps=deps,
                    work_id=work_id,
                    owner=owner,
                    stop=stop_heartbeat,
                    lease_lost=lease_lost,
                )
            )
        # One provider attempt only: central execution never auto-retries a
        # tool call. A retryable failure is returned to the model as feedback
        # for a legal follow-up decision.
        invocation_entered = True
        result = await _invoke_with_semaphore(
            deps,
            invocation,
            runtime=runtime,
            step=step,
            work_id=work_id,
            proposal=proposal,
            decision=decision,
        )
        any_provider_started = any_provider_started or (
            result.metadata.get(EXECUTION_STARTED_METADATA_KEY) is not False
        )
    except Exception as exc:  # noqa: BLE001 - normalize adapter/transport failure
        now = datetime.now(timezone.utc)
        if isinstance(exc, InvocationAdmissionDenied):
            admission_denial = exc.decision
            error_code = "policy_admission_denied"
            error_message = "current execution conditions no longer allow the action"
        elif not binding_ready:
            error_code = "tool_binding_invalid"
            error_message = "approved action has no valid healthy tool binding"
        elif not lease_ready:
            error_code = "lease_lost_before_execution"
            error_message = "work lease was unavailable before tool execution"
        else:
            error_code = f"gateway_exception:{type(exc).__name__}"
            error_message = "tool gateway raised an exception"
        orchestration_diagnostic = operational_exception_diagnostic(
            exc,
            stage="execute",
            code=error_code.replace(":", "_"),
            summary=error_message,
        )
        result = ToolResult(
            invocation_id=invocation.invocation_id,
            action_id=invocation.action_id,
            tool_id=invocation.tool_id,
            success=False,
            error=ToolError(
                code=error_code,
                # Exception text may contain target output or secrets.
                message=error_message,
                details={"stage": "execute", "source": "orchestration/execute"},
            ),
            started_at=now,
            finished_at=now,
            metadata={
                "orchestration_exception": True,
                EXECUTION_STARTED_METADATA_KEY: (
                    invocation_entered and not isinstance(exc, ToolStartRejected)
                ),
            },
        )
    finally:
        if heartbeat_task is not None:
            stop_heartbeat.set()
            with suppress(asyncio.CancelledError):
                await heartbeat_task

    if admission_denial is not None:
        state = {**state, "policy_decision": admission_denial}
        runtime = append_runtime_observation(
            runtime,
            RuntimeObservation(
                kind="control",
                summary="Current invocation admission denied",
                outcome=admission_denial.verdict.value,
                step=runtime.step_count,
                action_id=str(proposal.action_id),
                capability=proposal.capability,
                operation=proposal.operation,
                reasons=admission_denial.reasons[:8],
                matched_rules=admission_denial.matched_rules[:12],
                alternative_allowed=admission_denial.denial_severity != Severity.ABORT_RUN.value,
            ),
        )

    if lease_lost.is_set():
        result = result.model_copy(
            update={
                "success": False,
                "error": ToolError(
                    code="lease_lost_during_execution",
                    message="work lease could not be renewed while the tool was running",
                    retryable=False,
                ),
                "metadata": {
                    **result.metadata,
                    "orchestration_exception": True,
                    "lease_lost": True,
                },
            }
        )
    if any_provider_started and result.metadata.get(EXECUTION_STARTED_METADATA_KEY) is False:
        # Bounded retries share this invocation identity. A later queue
        # refusal cannot erase entry already observed for an earlier
        # attempt; the preserved result must carry the same accounting
        # basis as the runtime returned below.
        result = result.model_copy(update={
            "metadata": {**result.metadata, EXECUTION_STARTED_METADATA_KEY: True},
        })
    await append_execution_receipt(
        deps.evidence_store, invocation=invocation, proposal=proposal, step=step,
        work_id=work_id, reference=reference, result=result,
        target_node_id=(str(node_id_for_target(proposal.target))
                        if proposal.target is not None else None),
    )

    finished = await publisher.publish(
        EventType.TOOL_EXECUTION_FINISHED if result.success else EventType.TOOL_EXECUTION_FAILED,
        {
            "invocation_id": result.invocation_id,
            "action_id": result.action_id,
            "success": result.success,
            "error_code": result.error.code if result.error else None,
            "candidate_key": step.candidate_key,
            "hypothesis_id": proposal.hypothesis_id,
            **_native_lineage(step, runtime, state.get("current_work_id")),
        },
        correlation_id=correlation_id,
        causation_id=started.event_id if started is not None else state.get("last_event_id"),
    )

    # One action consumes one budget slot; there are no automatic retries.
    executed = int(
        any_provider_started or (
            result.metadata.get(EXECUTION_STARTED_METADATA_KEY) is not False
            and not (
                result.error
                and result.error.code == "policy_admission_denied"
                and retry_attempts == 0
            )
        )
    )
    consumed = executed + retry_attempts
    if runtime.remaining_budget is None:
        remaining_budget = None
    else:
        runtime.actions_executed + runtime.remaining_budget
        remaining_budget = max(runtime.remaining_budget - consumed, 0)
    actions_by_agent = dict(runtime.actions_by_agent)
    agent_key = str(proposal.agent_id)
    actions_by_agent[agent_key] = actions_by_agent.get(agent_key, 0) + executed

    # One projection point for returned tool failures: only the allowlisted
    # stage/exception_type travel from the failure detail into the next
    # judgment. Free-text messages and inputs never do.
    execution_diagnostic = orchestration_diagnostic
    if execution_diagnostic is None and result.error is not None:
        from redteam.orchestration.execution_projection import execution_outcome

        selection = None
        current_execution_id = state.get("current_execution_id")
        if getattr(deps, "central_state", None) is not None and current_execution_id:
            current_execution = await deps.central_state.get_execution(current_execution_id)
            if current_execution is not None:
                selection = current_execution.selection
        projected = execution_outcome(result, selection=selection)
        returned_details = result.error.details
        returned_stage = (
            returned_details.get("stage")
            if isinstance(returned_details, dict) and isinstance(returned_details.get("stage"), str)
            else "execute"
        )
        if returned_stage not in ("gateway", "execute"):
            returned_stage = "execute"
        returned_exception_type = (
            returned_details.get("exception_type")
            if isinstance(returned_details, dict)
            and isinstance(returned_details.get("exception_type"), str)
            else None
        )
        execution_diagnostic = DecisionDiagnostic(
            code=result.error.code[:100],
            stage=returned_stage,
            kind="execution",
            summary=f"Tool execution failed: {result.error.code}"[:500],
            retryable=bool(result.error.retryable),
            cause_scope=projected.cause_scope,
            owner="orchestration.execution_projection",
            repair_condition=projected.resume_condition,
            error_type=returned_exception_type[:100] if returned_exception_type else "",
        )

    next_runtime = append_runtime_observation(
        runtime.model_copy(
            update={
                "phase": AgentPhase.VERIFY,
                "actions_executed": runtime.actions_executed + executed,
                "consecutive_failures": (
                    runtime.consecutive_failures
                    if not executed
                    else 0
                    if result.success
                    else runtime.consecutive_failures + 1
                ),
                "remaining_budget": remaining_budget,
                "retry_count": retry_attempts,
                "retry_tool_calls": runtime.retry_tool_calls + retry_attempts,
                "actions_by_agent": actions_by_agent,
            }
        ),
        RuntimeObservation(
            kind="tool",
            summary=(
                f"Tool {result.tool_id} "
                + ("succeeded" if result.success else "failed")
                + (
                    f" with {result.error.code}: {result.error.message}"
                    if result.error is not None
                    else ""
                )
            )[:500],
            outcome="success" if result.success else "failed",
            step=runtime.step_count,
            action_id=str(proposal.action_id),
            capability=proposal.capability,
            operation=proposal.operation,
            reasons=([result.error.code, result.error.message] if result.error is not None else []),
            alternative_allowed=True,
            diagnostic=execution_diagnostic,
        ),
    )

    return advance(
        state,
        next_runtime,
        last_event_id=finished.event_id,
        tool_result=result,
    )
