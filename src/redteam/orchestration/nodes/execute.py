"""EXECUTE — only reachable after an ALLOW ``PolicyDecision``.

Runs the tool the approved ``PlanStep`` declared, and owns execution
accounting (budget, consecutive failures).
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta, timezone

from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.events import EventType
from redteam.core.contracts.tool import ToolError, ToolInvocation, ToolResult
from redteam.core.convert import build_tool_invocation
from redteam.core.enums import AgentPhase, RiskLevel, ToolHealthStatus
from redteam.orchestration.action_naming import (
    canonical_action_key,
    tool_matches_action_namespace,
)
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.nodes._support import advance, publisher_for
from redteam.orchestration.state import GraphState

DEFAULT_TIMEOUT_SECONDS = 10.0

# Risk LOW alone says nothing about idempotency (a low-risk auth attempt or
# upload can still mutate state). Until Capability has an explicit retry-safe
# descriptor contract, only known observation operations may be repeated.
_RETRY_SAFE_ACTIONS = frozenset(
    {
        "recon.service_discovery",
        "recon.endpoint_discovery",
        "analysis.response_analysis",
        "mobile.static_analysis",
    }
)


def _retry_safe(proposal: ActionProposal) -> bool:
    action = f"{proposal.capability}.{proposal.operation}"
    if action in _RETRY_SAFE_ACTIONS:
        return True
    if action == "web.http_request":
        method = proposal.arguments.get("method")
        return isinstance(method, str) and method.upper() in {"GET", "HEAD"} and not any(
            key in proposal.arguments for key in ("body", "data", "json", "payload")
        )
    return False


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


async def _invoke_with_semaphore(deps: RuntimeDeps, invocation: ToolInvocation) -> ToolResult:
    """Invoke the tool, honoring the run-wide execution bound.

    The semaphore is the *actual* ``RateLimitPolicy.max_concurrent_actions``
    gate: policy admission may be serialized, but approved tool calls can
    otherwise run in parallel across fan-out workers. ``None`` leaves
    execution unbounded (legacy).
    """

    semaphore = deps.tool_semaphore
    if semaphore is None:
        return await deps.tool_gateway.invoke(invocation)
    async with semaphore:
        return await deps.tool_gateway.invoke(invocation)


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
        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
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
        and _retry_safe(proposal)
    )
    max_retries = deps.settings.max_retries
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
    try:
        if not binding_ready:
            raise RuntimeError("tool binding is no longer valid for the approved action")
        if not lease_ready:
            raise RuntimeError("work lease was lost before tool execution")
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
            result = await _invoke_with_semaphore(deps, invocation)
            error = result.error
            if (
                result.success
                or not retry_enabled
                or retry_attempts >= max_retries
                or error is None
                or not error.retryable
            ):
                break
            # Each extra call needs an authoritative Control budget slot. The
            # policy lock serializes this reservation with fan-out admissions;
            # a missing optional extension keeps injected test doubles usable.
            reserve = getattr(deps.control_plane, "has_tool_call_capacity", None)
            charge = getattr(deps.control_plane, "record_tool_calls", None)
            if runtime.remaining_budget is not None and (
                runtime.remaining_budget <= 1 + retry_attempts
            ):
                break
            if callable(reserve) and callable(charge):
                async with deps.policy_lock:
                    if not await reserve(proposal.engagement_id):
                        break
                    await charge(proposal.engagement_id, 1)
            retry_attempts += 1
    except Exception as exc:  # noqa: BLE001 - normalize adapter/transport failure
        now = datetime.now(timezone.utc)
        if not binding_ready:
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
    actions_by_agent[agent_key] = actions_by_agent.get(agent_key, 0) + 1

    return advance(
        state,
        runtime.model_copy(
            update={
                "phase": AgentPhase.VERIFY,
                "actions_executed": runtime.actions_executed + 1,
                "consecutive_failures": 0 if result.success else runtime.consecutive_failures + 1,
                "remaining_budget": remaining_budget,
                "retry_count": retry_attempts,
                "retry_tool_calls": runtime.retry_tool_calls + retry_attempts,
                "actions_by_agent": actions_by_agent,
            }
        ),
        last_event_id=finished.event_id,
        tool_result=result,
    )
