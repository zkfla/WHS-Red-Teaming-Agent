"""EXECUTE — only reachable after an ALLOW ``PolicyDecision``.

Runs the tool the approved ``PlanStep`` declared, and owns execution
accounting (budget, consecutive failures).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import suppress
from datetime import datetime, timedelta, timezone

from redteam.attack_graph.identity import node_id_for_target
from redteam.core.contracts.events import EventType
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.contracts.policy import ControlContext, PolicyDecision
from redteam.core.contracts.tool import ToolError, ToolInvocation, ToolResult
from redteam.core.convert import build_tool_invocation
from redteam.core.enums import (
    AgentPhase,
    EvidenceSourceType,
    PolicyVerdict,
    RiskLevel,
    Severity,
    ToolHealthStatus,
)
from redteam.core.ids import EvidenceId, new_id
from redteam.core.ports.control_plane import InvocationAdmissionControl
from redteam.core.ports.tool_gateway import ToolStartAdmissionGateway, ToolStartRejected
from redteam.orchestration.action_naming import (
    canonical_action_key,
    tool_matches_action_namespace,
)
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.engine import claim_action_admission, validate_current_action
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


async def _invocation_context(deps, runtime, proposal, work_id, tool_id=None):
    items = await deps.work_store.list_items(run_id=runtime.run_id)
    others = sum(
        item.work_id != work_id and item.status in {WorkStatus.LEASED, WorkStatus.RUNNING}
        for item in items
    )
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
        if isinstance(deps.control_plane, InvocationAdmissionControl):
            context = await _invocation_context(
                deps, runtime, proposal, work_id, invocation.tool_id
            )
            async with deps.policy_lock:
                admitted = await deps.control_plane.admit_invocation(
                    proposal, decision, context, receipt_id=str(invocation.invocation_id)
                )
            if admitted.verdict is not PolicyVerdict.ALLOW:
                raise InvocationAdmissionDenied(admitted)

    async def invoke():
        if deps.long_job_adapter is None:
            if isinstance(deps.tool_gateway, ToolStartAdmissionGateway):
                return await deps.tool_gateway.invoke_with_admission(invocation, before_start)
            await before_start()
            return await deps.tool_gateway.invoke(invocation)
        if work_id is None:
            raise ValueError("long jobs require a durable execution record")
        manager = LongJobManager(deps.long_job_adapter)
        work = await deps.work_store.get(work_id)
        job_id = work.job_id if work else None
        if job_id is None:
            await before_start()
            job_id = await manager.submit(invocation)
            if not await deps.work_store.bind_job(
                work_id, owner=str(runtime.run_id), job_id=job_id
            ):
                raise RuntimeError("submitted job could not be recorded; reconcile before retry")
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
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError("registered job is still running; collect its result later")
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            # Keep the registered identity even if cancellation is unconfirmed.
            with suppress(Exception):
                await asyncio.shield(manager.cancel(job_id))
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

    started = await publisher.publish(
        EventType.TOOL_EXECUTION_STARTED,
        {
            "invocation_id": invocation.invocation_id,
            "action_id": invocation.action_id,
            "tool_id": invocation.tool_id,
            "candidate_key": step.candidate_key,
            "hypothesis_id": proposal.hypothesis_id,
        },
        correlation_id=correlation_id,
        causation_id=state.get("last_event_id"),
    )

    work_id = state.get("current_work_id")
    owner = str(runtime.run_id)
    lease_lost = asyncio.Event()
    heartbeat_task: asyncio.Task[None] | None = None
    lease_ready = True
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
            and proposal.capability in descriptor.capability_tags
            and descriptor.health_status is not ToolHealthStatus.UNHEALTHY
        )
    # A bounded retry needs a retryable gateway error, a known LOW-risk
    # descriptor *and* an explicitly read-only operation; LOW alone cannot
    # establish idempotency (for example, a login attempt mutates state).
    retry_enabled = bool(
        deps.settings.retry_retryable_tool_failures
        and deps.settings.max_retries > 0
        and descriptor is not None
        and descriptor.risk_level is RiskLevel.LOW
        and (
            descriptor.retry_safe
            or (
                deps.retry_policy is not None
                and deps.retry_policy.permits(
                    proposal.capability, proposal.operation, proposal.arguments
                )
            )
        )
        and isinstance(deps.control_plane, InvocationAdmissionControl)
        and deps.long_job_adapter is None
    )
    max_retries = deps.settings.max_retries
    if step.delegation_id:
        delegation = runtime.delegations.get(step.delegation_id)
        if delegation is None:
            binding_ready = False
        else:
            max_retries = min(
                max_retries,
                max(delegation.budget.max_actions - delegation.actions_used - 1, 0),
            )
    if work_id is not None:
        try:
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

    retry_attempts = 0
    admission_denial = None
    try:
        path_problems = await validate_current_action(deps, runtime, step, proposal)
        if path_problems:
            binding_ready = False
        if not binding_ready:
            raise RuntimeError("tool binding is no longer valid for the approved action")
        if not lease_ready:
            raise RuntimeError("work lease was lost before tool execution")
        # Persist the original invocation before submission, so an uncertain
        # job can be collected later without reconstructing or repeating it.
        if deps.long_job_adapter is not None:
            receipt = {
                "proposal": proposal.model_dump(mode="json"),
                "plan_step": step.model_dump(mode="json"),
                "invocation": invocation.model_dump(mode="json"),
                "candidate_key": step.candidate_key,
                "target_node_id": str(node_id_for_target(proposal.target)),
                "work_id": work_id,
                "receipt_kind": "execution_submission",
            }
            await deps.evidence_store.append(
                EvidenceRecord(
                    evidence_id=EvidenceId(new_id("evidence")),
                    run_id=runtime.run_id,
                    engagement_id=runtime.engagement_id,
                    source_type=EvidenceSourceType.AGENT,
                    source_id="orchestration.execute",
                    action_id=proposal.action_id,
                    invocation_id=invocation.invocation_id,
                    hypothesis_id=proposal.hypothesis_id,
                    content_type="application/json",
                    digest=hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest(),
                    summary="Registered original execution for job reconciliation",
                    created_at=datetime.now(timezone.utc),
                    metadata=receipt,
                )
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
        while True:
            result = await _invoke_with_semaphore(
                deps,
                invocation,
                runtime=runtime,
                step=step,
                work_id=work_id,
                proposal=proposal,
                decision=decision,
            )
            error = result.error
            if (
                result.success
                or not retry_enabled
                or retry_attempts >= max_retries
                or error is None
                or not error.retryable
            ):
                break
            if runtime.remaining_budget is not None and (
                runtime.remaining_budget <= 1 + retry_attempts
            ):
                break
            context = await _invocation_context(
                deps, runtime, proposal, work_id, invocation.tool_id
            )
            retry_control = deps.control_plane
            if not isinstance(retry_control, InvocationAdmissionControl):
                break
            async with deps.policy_lock:
                reservation = await retry_control.reserve_extra_tool_call(
                    proposal,
                    decision,
                    context,
                    receipt_id=str(invocation.invocation_id),
                    attempt_index=retry_attempts + 1,
                )
            if reservation.verdict is not PolicyVerdict.ALLOW:
                admission_denial = reservation
                break
            retry_attempts += 1
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
        result = ToolResult(
            invocation_id=invocation.invocation_id,
            action_id=invocation.action_id,
            tool_id=invocation.tool_id,
            success=False,
            error=ToolError(
                code=error_code,
                # Exception text may contain target output or secrets.
                message=error_message,
            ),
            started_at=now,
            finished_at=now,
            metadata={"orchestration_exception": True},
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
    if retry_attempts:
        # Bounded-retry observability: how many extra attempts this action
        # consumed (the final ToolResult is the one recorded as Evidence).
        result = result.model_copy(
            update={"metadata": {**result.metadata, "retry_attempts": retry_attempts}}
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
        },
        correlation_id=correlation_id,
        causation_id=started.event_id,
    )

    # An action consumes one budget slot; each retry attempt consumes an
    # extra slot so a bounded retry cannot silently exceed the run budget
    # (the Control Plane's own budget is charged once per admitted action).
    consumed = 1 + retry_attempts
    executed = int(
        not (
            result.error and result.error.code == "policy_admission_denied" and retry_attempts == 0
        )
    )
    if runtime.remaining_budget is None:
        remaining_budget = None
    else:
        total_budget = runtime.actions_executed + runtime.remaining_budget
        remaining_budget = max(runtime.remaining_budget - consumed, 0)
        if deps.settings.enforce_verification_reserve:
            reserve = deps.settings.verification_reserve(total_budget)
            if remaining_budget <= reserve:
                # Leave the reserved slice for verification; the loop stops
                # with that headroom untouched.
                remaining_budget = 0
    actions_by_agent = dict(runtime.actions_by_agent)
    agent_key = str(proposal.agent_id)
    actions_by_agent[agent_key] = actions_by_agent.get(agent_key, 0) + executed

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
        ),
    )

    return advance(
        state,
        next_runtime,
        last_event_id=finished.event_id,
        tool_result=result,
    )
