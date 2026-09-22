"""EXECUTE — only reachable after an ALLOW ``PolicyDecision``.

Runs the tool the approved ``PlanStep`` declared, and owns execution
accounting (budget, consecutive failures).
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta, timezone

from redteam.core.contracts.events import EventType
from redteam.core.contracts.tool import ToolError, ToolResult
from redteam.core.convert import build_tool_invocation
from redteam.core.enums import AgentPhase, ToolHealthStatus
from redteam.orchestration.action_naming import (
    canonical_action_key,
    tool_matches_action_namespace,
)
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.nodes._support import advance, publisher_for
from redteam.orchestration.state import GraphState

DEFAULT_TIMEOUT_SECONDS = 10.0


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
    if binding_ready and deps.tool_catalog is not None:
        descriptor = deps.tool_catalog.get(step.tool_id)
        binding_ready = bool(
            descriptor is not None
            and proposal.capability in descriptor.capability_tags
            and descriptor.health_status is not ToolHealthStatus.UNHEALTHY
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
        result = await deps.tool_gateway.invoke(invocation)
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

    remaining_budget = (
        None if runtime.remaining_budget is None else max(runtime.remaining_budget - 1, 0)
    )
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
                "actions_by_agent": actions_by_agent,
            }
        ),
        last_event_id=finished.event_id,
        tool_result=result,
    )
