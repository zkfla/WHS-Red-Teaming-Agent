"""Result collection for registered jobs; never submit another execution.

Central runs resolve against their own execution ledger and the
existing original-lookup port. The central path never restores
WorkStore or Work ids; work_id input on a central Run is an explicit
compatibility error naming the supported job_ref/invocation_id
references. Listing returns masked status/reference rows only;
collection reuses receipt verification and the existing commit
contract, keeps active/UNKNOWN held, never resubmits a job, and never
confirms Task/goal success.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from redteam.orchestration.cognition import DecisionFailure, ReasoningHalted
from redteam.orchestration.diagnostics import (
    diagnostic_for_exception,
    operational_exception_diagnostic,
)
from redteam.orchestration.local_tools import (
    LocalToolContext,
    LocalToolDescriptor,
    LocalToolResult,
    runtime_tool_contribution,
)


class JobReconcileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    work_id: str | None = Field(default=None, min_length=1)
    job_ref: str | None = Field(default=None, min_length=1)
    invocation_id: str | None = Field(default=None, min_length=1)


async def job_reconcile_handler(arguments: dict[str, Any], context: LocalToolContext):
    try:
        request = JobReconcileInput.model_validate(arguments)
    except (ValueError, TypeError) as exc:
        diagnostic = diagnostic_for_exception(exc, stage="native_local_tool")
        return LocalToolResult(ok=False, error=diagnostic.feedback())
    deps, runtime = context.deps, context.runtime
    if getattr(deps, "central_state", None) is not None:
        return await _central_job_reconcile(request, context)
    if deps.long_job_adapter is None:
        return LocalToolResult(ok=False, error="registered job collection is not supported")
    from redteam.orchestration.engine import InvestigationEngine

    engine = None
    try:
        if request.work_id is None:
            jobs = [
                {"work_id": item.work_id, "job_id": item.job_id, "status": item.status.value}
                for item in await deps.work_store.list_items(run_id=runtime.run_id)
                if item.instance_id == runtime.instance_id
                and item.job_id and not (item.outcome_reason or "").startswith("job_reconciled:")
            ]
            return LocalToolResult(ok=True, payload={"jobs": jobs})
        work = await deps.work_store.get(request.work_id)
        if (work is None or work.run_id != runtime.run_id
            or work.instance_id != runtime.instance_id or not work.job_id):
            return LocalToolResult(
                ok=False, error="no registered job for this run",
                payload={"status": "no_registered_job"},
            )
        if (work.outcome_reason or "").startswith("job_reconciled:"):
            return LocalToolResult(
                ok=True, payload={"status": "already_collected", "work_id": work.work_id},
            )
        engine = InvestigationEngine(deps)
        engine.begin_native_turn(runtime)
        engine.last_event_id = context.last_event_id
        result = await engine.reconcile_job(
            {"work_id": request.work_id}, runtime, budget=context.budget,
        )
        status = result["status"]
        ok = status in {"collected", "pending", "running", "unknown"}
        refs = (str(result["evidence_id"]),) if result.get("evidence_id") else ()
        return LocalToolResult(
            ok=ok, payload=result, refs=refs,
            error=None if ok else "registered job collection did not complete: " + status,
            last_event_id=engine.last_event_id if engine else context.last_event_id,
        )
    except (DecisionFailure, ReasoningHalted):
        raise
    except Exception as exc:
        diagnostic = operational_exception_diagnostic(
            exc, stage="native_local_tool", code="job_collection_failed",
            summary="The registered job result could not be collected.",
        )
        return LocalToolResult(
            ok=False, error=diagnostic.feedback(),
            payload={"diagnostic": diagnostic.model_dump(mode="json")},
            last_event_id=engine.last_event_id if engine else context.last_event_id,
        )


_CENTRAL_HELD_STATUSES = frozenset({"pending", "running", "unknown"})


def _masked_job_row(execution):
    row = {
        "job_ref": execution.job_ref,
        "invocation_id": str(execution.invocation_id),
        "action_id": str(execution.action_id),
        "task_id": str(execution.task_id),
        "instance_id": str(execution.instance_id),
        "state": execution.state.value,
        "wait_reason": execution.wait_reason,
    }
    return row


def _central_no_job(status, error):
    return LocalToolResult(ok=False, error=error, payload={"status": status})


async def _central_job_reconcile(request, context):
    from types import SimpleNamespace

    from redteam.core.contracts.execution import ExecutionState

    deps, runtime = context.deps, context.runtime
    state = deps.central_state
    run_id = str(runtime.run_id)
    if (request.work_id is not None and request.job_ref is None
            and request.invocation_id is None):
        return _central_no_job(
            "work_id_not_supported_centrally",
            "central job lookup uses job_ref/invocation_id, not work_id",
        )
    if request.job_ref is None and request.invocation_id is None:
        executions = await state.list_executions()
        jobs = [
            _masked_job_row(item)
            for item in executions
            if item.job_ref
            and str(item.run_id) == run_id
            and str(item.instance_id) == str(runtime.instance_id)
        ]
        payload = {"jobs": jobs}
        if deps.long_job_adapter is None:
            payload["long_job_adapter"] = "unavailable: no adapter at the assembly point"
        else:
            payload["long_job_adapter"] = "bound"
        return LocalToolResult(ok=True, payload=payload)
    if request.invocation_id is not None:
        execution = await state.get_execution(request.invocation_id)
        if execution is None:
            return _central_no_job("no_registered_job", "no registered job for this run")
    else:
        executions = await state.list_executions()
        matches = [item for item in executions if item.job_ref == request.job_ref]
        if not matches:
            return _central_no_job("no_registered_job", "no registered job for this run")
        if len(matches) > 1:
            return _central_no_job("ambiguous_job_ref", "ambiguous job reference for this run")
        execution = matches[0]
    if str(execution.run_id) != run_id:
        return _central_no_job("no_registered_job", "no registered job for this run")
    if str(execution.instance_id) != str(runtime.instance_id):
        return _central_no_job("wrong_owner_ref", "job is registered to another instance")
    active_task = getattr(runtime, "active_task_id", None)
    if active_task is not None and str(execution.task_id) != str(active_task):
        return _central_no_job("wrong_task_ref", "job is registered to another task")
    if not execution.job_ref:
        terminal = {ExecutionState.SUCCEEDED, ExecutionState.FAILED, ExecutionState.CANCELLED}
        settled = execution.state in terminal and execution.result_evidence_id is not None
        if not settled:
            return _central_no_job("no_registered_job", "registered execution has no job reference")
    if execution.state in {ExecutionState.SUCCEEDED, ExecutionState.FAILED,
                            ExecutionState.CANCELLED}:
        if execution.result_evidence_id is not None:
            return LocalToolResult(ok=True, payload={
                "status": "already_collected",
                "job_ref": execution.job_ref,
                "invocation_id": str(execution.invocation_id),
                "result_evidence_id": str(execution.result_evidence_id),
            })
    try:
        from redteam.core.contracts.action import ActionProposal
        from redteam.core.enums import AgentPhase
        from redteam.orchestration.agents.base import PlanStep
        from redteam.orchestration.central_recovery import collect_original
        from redteam.orchestration.central_runtime import commit_result, runtime_token
        from redteam.orchestration.central_state import check_evidence, check_execution_receipt
        from redteam.orchestration.coordination import CoordinationError, StateConflict
        from redteam.orchestration.execution_journal import ExecutionReceiptError, checked_receipt
        from redteam.orchestration.state import initial_graph_state

        await runtime_token(deps, runtime, admit=False)
        task = await state.get_task(str(execution.task_id))
        if task is None:
            return _central_no_job("wrong_task_ref", "registered job task is missing")
        checked = await check_evidence(
            deps.evidence_store, run_id=state.run_id, engagement_id=state.engagement_id,
            evidence_ids=[str(execution.proposal_evidence_id)],
        )
        submission = checked[0].record
        check_execution_receipt(execution, submission, result=False, task=task)
        original = await collect_original(deps, execution, submission)
        if original is None:
            row = _masked_job_row(execution)
            if deps.long_job_adapter is None:
                row["long_job_adapter"] = "unavailable: no adapter at the assembly point"
            status = execution.state.value
            held = status in _CENTRAL_HELD_STATUSES
            detail = {"status": status}
            detail.update(row)
            return LocalToolResult(
                ok=held,
                payload=detail,
                error=None if held else "registered job collection did not complete: " + status,
            )
        result, receipt_id = original
        data = checked_receipt(submission)
        dispatch = SimpleNamespace(
            proposal=ActionProposal.model_validate(data["proposal"]),
            plan_step=PlanStep.model_validate(data["plan_step"]),
        )
        local = initial_graph_state(runtime.model_copy(update={
            "phase": AgentPhase.VERIFY, "termination_reason": None,
        }))
        local.update(action_proposal=dispatch.proposal, plan_step=dispatch.plan_step,
                     tool_result=result)
        await commit_result(deps, local, dispatch)
        return LocalToolResult(ok=True, payload={
            "status": "collected",
            "job_ref": execution.job_ref,
            "invocation_id": str(execution.invocation_id),
            "action_id": str(execution.action_id),
            "task_id": str(execution.task_id),
            "result_evidence_id": str(receipt_id),
        }, refs=(str(receipt_id),))
    except (DecisionFailure, ReasoningHalted):
        raise
    except ExecutionReceiptError as exc:
        return _central_no_job(
            exc.reason_code, "registered job original rejected: " + exc.reason_code,
        )
    except (CoordinationError, StateConflict) as exc:
        return _central_no_job(str(exc), "registered job collection held: " + str(exc))
    except Exception as exc:
        diagnostic = operational_exception_diagnostic(
            exc, stage="native_local_tool", code="job_collection_failed",
            summary="The registered job result could not be collected.",
        )
        return LocalToolResult(
            ok=False, error=diagnostic.feedback(),
            payload={"diagnostic": diagnostic.model_dump(mode="json")},
        )


def job_reconciliation_contribution():
    def factory(wiring):
        return LocalToolDescriptor(
            tool_id="job.reconcile",
            description=(
                "List this run's registered jobs with no arguments, or provide job_ref "
                "or invocation_id to collect an existing job result without submitting "
                "or repeating the tool. Pending, running and unknown outcomes remain "
                "unresolved. work_id is legacy-only and rejected on central runs."
            ),
            input_schema=JobReconcileInput.model_json_schema(),
            contribution_id="job-reconciliation",
        ), job_reconcile_handler

    return runtime_tool_contribution(contribution_id="job-reconciliation", factory=factory)
