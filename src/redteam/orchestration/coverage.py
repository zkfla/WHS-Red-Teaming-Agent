"""Read-only coverage projection over current goal conditions and receipts.

This never judges completion: it only separates current goal gaps from past
performance so reporting and the next judgment read the same contract.
Completion authority stays with the completion path; nothing here promotes
a new record into an observed fact or a verdict ground.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from redteam.orchestration.result_claims import mission_result_purpose

__all__ = [
    "GoalConditionCoverage",
    "TaskCoverage",
    "CoverageGapSummary",
    "project_coverage_gaps",
]


class GoalConditionCoverage(BaseModel):
    """One original goal condition and whether evidence says it is met.

    met is None when the condition was never evaluated: unevaluated counts
    as a gap, never as success.
    """

    model_config = ConfigDict(frozen=True)

    criterion: str
    met: bool | None = None
    evidence_count: int = 0
    reason: str | None = None


class TaskCoverage(BaseModel):
    """One mission task and whether a completion receipt backs it.

    has_completion_receipt only reports that the task record references a
    receipt; it never validates the receipt itself.
    """

    model_config = ConfigDict(frozen=True)

    task_id: str
    required: bool = True
    status: str = "unknown"
    has_completion_receipt: bool = False


class CoverageGapSummary(BaseModel):
    """Current goal gaps beside past performance, read-only.

    version stays so older stored reports without this projection read as
    unknown instead of complete. Past performance (actions, evidence and
    finding counts) lives on the terminal report itself, never here.
    """

    model_config = ConfigDict(frozen=True)

    version: str = "1"
    conditions: list[GoalConditionCoverage] = Field(default_factory=list)
    unmet_conditions: list[str] = Field(default_factory=list)
    tasks: list[TaskCoverage] = Field(default_factory=list)
    required_incomplete_tasks: list[str] = Field(default_factory=list)
    completed_without_receipt: list[str] = Field(default_factory=list)
    unknown: bool = False


def _task_status(task: Any) -> str:
    status = getattr(task, "status", None)
    return str(getattr(status, "value", status))


def project_coverage_gaps(*, mission: Any | None, tasks: list[Any]) -> CoverageGapSummary:
    """Project gaps from the current mission and task records, read-only.

    mission supplies the original success_criteria and criterion_results;
    tasks supply required/status/completion_evidence_id. Nothing is written,
    promoted or re-judged: a completed task without a receipt reference is
    reported as legacy, never given a fresh receipt here.
    """
    if mission is None:
        return CoverageGapSummary(unknown=True)
    results = {
        str(item.criterion): item
        for item in (getattr(mission, "criterion_results", None) or [])
        if getattr(item, "criterion", None) is not None
    }
    conditions: list[GoalConditionCoverage] = []
    unmet: list[str] = []
    for criterion in list(getattr(mission, "success_criteria", None) or []):
        name = str(criterion)
        matched = results.get(name)
        if matched is not None and getattr(matched, "purpose_hash", None) is not None:
            if matched.purpose_hash != mission_result_purpose(mission):
                matched = None
        if matched is None:
            conditions.append(GoalConditionCoverage(criterion=name))
            unmet.append(name)
            continue
        met = bool(getattr(matched, "met", False))
        conditions.append(
            GoalConditionCoverage(
                criterion=name,
                met=met,
                evidence_count=len(list(getattr(matched, "evidence_ids", None) or [])),
                reason=getattr(matched, "reason", None),
            )
        )
        if not met:
            unmet.append(name)
    incomplete: list[str] = []
    no_receipt: list[str] = []
    covered: list[TaskCoverage] = []
    for task in list(tasks or []):
        task_id = str(getattr(task, "task_id", ""))
        required = bool(getattr(task, "required", True))
        status = _task_status(task)
        receipt = getattr(task, "completion_evidence_id", None)
        covered.append(
            TaskCoverage(
                task_id=task_id,
                required=required,
                status=status,
                has_completion_receipt=receipt is not None,
            )
        )
        if required and status != "completed":
            incomplete.append(task_id)
        if status == "completed" and receipt is None:
            no_receipt.append(task_id)
    return CoverageGapSummary(
        conditions=conditions,
        unmet_conditions=sorted(set(unmet)),
        tasks=covered,
        required_incomplete_tasks=sorted(set(incomplete)),
        completed_without_receipt=sorted(set(no_receipt)),
    )
