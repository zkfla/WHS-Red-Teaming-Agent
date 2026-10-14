"""Native-loop runtime-local evidence and settlement helpers (B0).

A runtime-local tool runs inside the execution context, spends the caller
reasoning budget, and returns through the generic model tool_calls path,
so the center never branches on tool names. This module declares the
three minimum native-loop helpers; the root generates provider-exposed
names from their descriptors.

- evidence.read reuses the existing bounded original-evidence read
  (EvidenceReader window plus the retained read list). Raw payloads stay
  in the store; only the requested window is returned.
- task.settle and goal.settle run the canonical settlement worker
  (semantic_completion complete_task and complete_goal) with path None.
  The current task, purpose, and mission come from the run, never from a
  model-supplied path. Only a successful settlement sets
  LocalToolResult.runtime_outcome, using the existing outcome vocabulary.

Counter semantics: settlement handlers share the caller DecisionBudget
with the review calls they trigger and never touch runtime model
counters themselves. The inner engine review counts its usage into the
shared budget and, with the run attached for audit events, directly into
the runtime counters. The dispatch owner must therefore reconcile with
before/after snapshots plus max-delta instead of re-adding budget
totals, or the review calls count twice.
"""

from __future__ import annotations

from typing import Any

from redteam.core.extensions import Contribution
from redteam.orchestration.cognition import DecisionFailure, ReasoningHalted
from redteam.orchestration.completion_proposal import CompletionProposal
from redteam.orchestration.diagnostics import (
    diagnostic_for_exception,
    operational_exception_diagnostic,
)
from redteam.orchestration.evidence_access import (
    EvidenceReader,
    EvidenceReadRequest,
    read_for_runtime,
)
from redteam.orchestration.local_tools import (
    RUNTIME_TOOL_CONTRIBUTION_VERSION,
    LocalToolContext,
    LocalToolDescriptor,
    LocalToolResult,
    runtime_tool_contribution,
)

EVIDENCE_READ_TOOL_ID = "evidence.read"
TASK_SETTLE_TOOL_ID = "task.settle"
GOAL_SETTLE_TOOL_ID = "goal.settle"

EVIDENCE_READ_CONTRIBUTION_ID = "evidence-read"
TASK_SETTLE_CONTRIBUTION_ID = "task-settle"
GOAL_SETTLE_CONTRIBUTION_ID = "goal-settle"

_READ_ERRORS = {
    "not_found": "evidence not found",
    "run_mismatch": "evidence belongs to another run",
    "no_raw": "evidence has no readable output",
}


async def evidence_read_handler(
    tool_input: dict[str, Any], context: LocalToolContext
) -> LocalToolResult:
    try:
        request = EvidenceReadRequest.model_validate(tool_input or {})
    except Exception as exc:
        # Validation detail stays safe and specific: the shared diagnostic
        # names the known field and rule without echoing raw inputs or
        # unknown key names. This never widens what the reader accepts.
        diagnostic = diagnostic_for_exception(exc, stage="native_local_tool")
        diagnostic = diagnostic.model_copy(update={
            "cause_scope": "model_input", "owner": "orchestration.evidence_access",
            "repair_condition": "Correct the evidence read arguments using the registered schema.",
        })
        return LocalToolResult(
            ok=False,
            error=diagnostic.feedback(),
            payload={"diagnostic": diagnostic.model_dump(mode="json")},
        )
    deps = getattr(context, "deps", None)
    runtime = getattr(context, "runtime", None)
    store = getattr(deps, "evidence_store", None)
    run_id = str(getattr(runtime, "run_id", "") or "")
    if store is None or not run_id.strip():
        return LocalToolResult(ok=False, error="evidence read needs store and run")
    delegation = getattr(context, "delegation", None)
    delegation_id = getattr(delegation, "delegation_id", None)
    if delegation_id is not None and not isinstance(delegation_id, str):
        delegation_id = None
    try:
        if isinstance(getattr(runtime, "evidence_reads", None), list):
            result = await read_for_runtime(
                store, runtime, request, delegation_id=delegation_id
            )
        else:
            result = await EvidenceReader(store).read(
                run_id=run_id,
                engagement_id=str(getattr(runtime, "engagement_id", ""))
                if getattr(runtime, "engagement_id", None)
                else None,
                **request.model_dump(
                    include={"evidence_id", "offset", "limit", "query"}
                ),
            )
    except (ReasoningHalted, DecisionFailure):
        # Reasoning terminal signals belong to the caller (_local_call),
        # which records the diagnostic and halts the run. Converting them
        # here would hide the halt as an ordinary tool failure.
        raise
    except Exception as exc:
        # Runtime store failures keep the existing safe operational
        # contract: fixed summary plus exception type names only, never
        # raw inputs or store contents. The serialized diagnostic rides in
        # the payload so storage and reports can consume it.
        diagnostic = operational_exception_diagnostic(
            exc,
            stage="native_local_tool",
            code="evidence_read_failed",
            summary="The stored evidence could not be read.",
        )
        return LocalToolResult(
            ok=False,
            payload={"diagnostic": diagnostic.model_dump(mode="json")},
            error=diagnostic.feedback(),
        )
    status = str(result.get("status") or "")
    if status == "ok":
        return LocalToolResult(
            ok=True,
            payload=dict(result),
            refs=(str(request.evidence_id),),
            evidence_read_request=request.model_dump(
                include={"evidence_id", "offset", "limit", "query"},
            ),
        )
    return LocalToolResult(
        ok=False,
        payload=dict(result),
        error=_READ_ERRORS.get(status, "evidence read unavailable"),
    )


async def _settle(
    scope: str, tool_input: dict[str, Any], context: LocalToolContext
) -> LocalToolResult:
    try:
        proposal = CompletionProposal.model_validate(tool_input or {})
    except Exception as exc:
        # Same shared validation contract as the evidence reader: known
        # fields and rules only, never raw values or unknown key names.
        diagnostic = diagnostic_for_exception(exc, stage="native_local_tool")
        diagnostic = diagnostic.model_copy(update={
            "cause_scope": "model_input", "owner": "orchestration.completion_proposal",
            "repair_condition": "Correct the completion arguments using the registered schema.",
        })
        return LocalToolResult(
            ok=False,
            error=diagnostic.feedback(),
            payload={"diagnostic": diagnostic.model_dump(mode="json")},
        )
    deps = getattr(context, "deps", None)
    runtime = getattr(context, "runtime", None)
    run_id = str(getattr(runtime, "run_id", "") or "")
    if deps is None or runtime is None or not run_id.strip():
        return LocalToolResult(ok=False, error="settlement needs stores and run")
    if scope == "task" and not str(
        getattr(runtime, "active_task_id", "") or ""
    ).strip():
        return LocalToolResult(
            ok=False, error="task settlement needs an active mission task"
        )
    if scope == "goal" and getattr(runtime, "mission_id", None) is None:
        return LocalToolResult(
            ok=False, error="goal settlement needs an operator mission"
        )
    from redteam.orchestration.central_results import settle_central

    return await settle_central(scope, proposal, context)


async def task_settle_handler(
    tool_input: dict[str, Any], context: LocalToolContext
) -> LocalToolResult:
    return await _settle("task", tool_input, context)


async def goal_settle_handler(
    tool_input: dict[str, Any], context: LocalToolContext
) -> LocalToolResult:
    return await _settle("goal", tool_input, context)


def evidence_read_descriptor() -> LocalToolDescriptor:
    return LocalToolDescriptor(
        tool_id=EVIDENCE_READ_TOOL_ID,
        description=(
            "Read a bounded evidence window for this run and engagement (evidence_id, "
            "offset, limit, query). view_kind distinguishes execution_original from "
            "runtime_diagnostic, review and unresolved DATA. Diagnostic reads prove no "
            "execution or Finding; review admission still requires its original binding gates. "
            "no_raw explains unsupported views or absent originals; rereading changes neither."
        ),
        input_schema=EvidenceReadRequest.model_json_schema(),
        contribution_id=EVIDENCE_READ_CONTRIBUTION_ID,
        version="v3", initial_exposure=True,
    )


def task_settle_descriptor() -> LocalToolDescriptor:
    return LocalToolDescriptor(
        tool_id=TASK_SETTLE_TOOL_ID,
        description=(
            "Settle the current mission task from stored evidence: "
            "success or refuted closes it completed; inconclusive or "
            "blocked records a canonical unresolved gap and blocks it. "
            "Provide outcome, reason, the stored evidence ids that prove "
            "it, and gaps for unresolved results. The current task and its "
            "purpose come from the run; no path is required."
        ),
        input_schema=CompletionProposal.model_json_schema(),
        contribution_id=TASK_SETTLE_CONTRIBUTION_ID,
        version="v3", initial_exposure=True,
    )


def goal_settle_descriptor() -> LocalToolDescriptor:
    return LocalToolDescriptor(
        tool_id=GOAL_SETTLE_TOOL_ID,
        description=(
            "Settle the original mission from stored evidence: success or "
            "refuted requires every required task settled and the operator "
            "criteria covered; inconclusive or blocked records a canonical "
            "unresolved gap and finishes the investigation. After the last "
            "task closes completed, the mission still needs this call."
        ),
        input_schema=CompletionProposal.model_json_schema(),
        contribution_id=GOAL_SETTLE_CONTRIBUTION_ID,
        version=RUNTIME_TOOL_CONTRIBUTION_VERSION,
    )


def evidence_read_contribution() -> Contribution:
    def factory(wiring: Any) -> tuple[LocalToolDescriptor, Any]:
        return evidence_read_descriptor(), evidence_read_handler

    return runtime_tool_contribution(
        contribution_id=EVIDENCE_READ_CONTRIBUTION_ID,
        version="v3", factory=factory,
    )


def task_settle_contribution() -> Contribution:
    def factory(wiring: Any) -> tuple[LocalToolDescriptor, Any]:
        return task_settle_descriptor(), task_settle_handler

    return runtime_tool_contribution(
        contribution_id=TASK_SETTLE_CONTRIBUTION_ID,
        version="v3", factory=factory,
    )


def goal_settle_contribution() -> Contribution:
    def factory(wiring: Any) -> tuple[LocalToolDescriptor, Any]:
        return goal_settle_descriptor(), goal_settle_handler

    return runtime_tool_contribution(
        contribution_id=GOAL_SETTLE_CONTRIBUTION_ID,
        factory=factory,
    )


def native_local_tool_contributions() -> tuple[Contribution, ...]:
    return (
        evidence_read_contribution(),
        task_settle_contribution(),
        goal_settle_contribution(),
    )
