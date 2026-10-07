"""Result collection for registered jobs; never submit another execution."""

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


async def job_reconcile_handler(arguments: dict[str, Any], context: LocalToolContext):
    try:
        request = JobReconcileInput.model_validate(arguments)
    except (ValueError, TypeError) as exc:
        diagnostic = diagnostic_for_exception(exc, stage="native_local_tool")
        return LocalToolResult(ok=False, error=diagnostic.feedback())
    deps, runtime = context.deps, context.runtime
    if deps.long_job_adapter is None:
        return LocalToolResult(ok=False, error="registered job collection is not supported")
    from redteam.orchestration.engine import InvestigationEngine

    engine = None
    try:
        if request.work_id is None:
            jobs = [
                {"work_id": item.work_id, "job_id": item.job_id, "status": item.status.value}
                for item in await deps.work_store.list_items(run_id=runtime.run_id)
                if item.job_id and not (item.outcome_reason or "").startswith("job_reconciled:")
            ]
            return LocalToolResult(ok=True, payload={"jobs": jobs})
        work = await deps.work_store.get(request.work_id)
        if work is None or work.run_id != runtime.run_id or not work.job_id:
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


def job_reconciliation_contribution():
    def factory(wiring):
        return LocalToolDescriptor(
            tool_id="job.reconcile",
            description=(
                "List this run's registered jobs with no arguments, or provide work_id to "
                "collect an existing job result without submitting or repeating the tool. "
                "Pending, running and unknown outcomes remain unresolved."
            ),
            input_schema=JobReconcileInput.model_json_schema(),
            contribution_id="job-reconciliation",
        ), job_reconcile_handler

    return runtime_tool_contribution(contribution_id="job-reconciliation", factory=factory)
