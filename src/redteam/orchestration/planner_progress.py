"""Typed planner progress: the canonical outcome of one hypothesis judgment.

Owned by orchestration. The projection is derived by the central judgment commit, in the
same SQLite transaction as the judgment, cursor, candidate/Task writes and any follow-up
wake. It is stored under the progress key of the existing planner_termination payload and
read only through the central transaction. Exhausted and goal-proposal judgments store no
projection: their original termination and the separate completion gate stay authoritative.
"""

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from redteam.orchestration.diagnostics import DecisionDiagnostic

#: The existing planning-invalidation wake kind; PROBE_WAKE_EVENTS already includes it.
PLANNING_INVALIDATED = "HypothesisPlanningInvalidated"
#: Code-owned reason for a follow-up accepted by the durable replan budget.
REPLAN_REQUESTED_REASON: Final = "planner_replan_requested"
#: Durable absolute count of accepted replans, one row per Run in state_requests.
REPLAN_BUDGET_KIND = "planner_replan_budget"
#: Code-owned diagnostic code for a replan denied by the cap.
REPLAN_EXHAUSTED_CODE = "planner_replan_exhausted"

PlannerProgressKind = Literal[
    "work_proposed",
    "existing_work",
    "replan_queued",
    "replan_exhausted",
    "held",
]


class PlannerProgressStatus(BaseModel):
    """What the latest judgment did to planning progress, with safe fields only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: PlannerProgressKind
    #: Absolute accepted replans for the Run after this judgment (never refilled).
    replan_count: int = Field(default=0, ge=0)
    replan_limit: int = Field(default=0, ge=0)
    #: Set only for replan_queued: the code-owned reason of the queued wake.
    cause_code: Literal["planner_replan_requested"] | None = None
    #: Set only for replan_exhausted: a concrete nonretryable no-progress diagnostic.
    diagnostic: DecisionDiagnostic | None = None

    @model_validator(mode="after")
    def _kind_matches_fields(self) -> "PlannerProgressStatus":
        if self.replan_count > self.replan_limit:
            raise ValueError("replan_count_exceeds_limit")
        if self.kind == "replan_queued":
            if self.cause_code != REPLAN_REQUESTED_REASON or self.diagnostic is not None:
                raise ValueError("replan_queued_requires_cause_only")
        elif self.kind == "replan_exhausted":
            diagnostic = self.diagnostic
            if (
                diagnostic is None
                or diagnostic.code != REPLAN_EXHAUSTED_CODE
                or diagnostic.retryable
                or self.cause_code is not None
            ):
                raise ValueError("replan_exhausted_requires_nonretryable_diagnostic")
        elif self.cause_code is not None or self.diagnostic is not None:
            raise ValueError("progress_kind_carries_no_cause_or_diagnostic")
        return self


def replan_exhausted_diagnostic(*, accepted: int, limit: int) -> DecisionDiagnostic:
    """Safe, concrete and nonretryable: it names the cap and the condition that can resume."""
    return DecisionDiagnostic(
        code=REPLAN_EXHAUSTED_CODE,
        stage="hypothesis_planning",
        kind="no_progress",
        attempts=accepted,
        attempt_limit=limit,
        retryable=False,
        owner="orchestration",
        summary=(
            "This judgment queued no follow-up because the Run's internal replan allowance is "
            "spent. Planning resumes only from a new original result, operator change or other "
            "current-state change."
        ),
        repair_condition=(
            "A new original task result, evidence change or operator instruction must arrive. "
            "The replan allowance does not refill."
        ),
    )
